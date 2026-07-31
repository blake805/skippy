"""Function-scoped disassembly and decompilation, over rizin and rz-ghidra.

Two tools: `disassemble_function` and `decompile`. Each takes a symbol and returns one
function. See [ADR 0018](docs/adr/0018-rizin-structured-tools.md) for the decision; the
two things worth knowing before reading the code are below.

**rizin is never allowlisted, and cannot be.** `run_command` in RE mode works from a
table of programs and forbidden flags, which is the right shape for `otool` and useless
for rizin, because rizin's `-c` argument is a command language of its own. `!cmd` runs a
shell command and `| prog` pipes output to an arbitrary program, so a single allowlisted
`rizin` would convert the inspection lane into a general execution lane. rizin therefore
appears in no rules table, and every invocation is an argument vector built here.

Three defences hold that up, and each is asserted by a test rather than left as a
convention:

1. `-N` on every invocation. rizin executes `~/.rizinrc` at startup and that file may
   contain `!`, so anything able to write one file into the home directory would get
   arbitrary code execution on the next RE session. This is the same chain that makes
   binwalk 2.x's path traversal an RCE rather than a nuisance, and the extractor in
   ADR 0019 is about to point format parsers at hostile blobs. The flag is the defence.
2. The model's symbol never enters a command string. It is resolved to an address
   against the binary's own symbol table first, and what gets interpolated is an
   integer this module formatted. There is no quoting question to get wrong, because
   there is nothing of the model's left to quote.
3. No `-w`, ever. Without it rizin cannot write to the artifact it opened.

**One function, not a region.** ADR 0007 concluded that what makes the 480B affordable
is keeping its context small and pre-digested; ADR 0012 then sent whole `objdump`
regions into it. `pdf` and `pdg` are function-scoped natively, so the useful shape and
the natural shape agree here. Analysis is scoped too: `af` at the resolved address
rather than `aa` over the whole binary, which on a firmware image is the difference
between a second and a coffee break.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from typing import Any, Dict, List, Optional, Tuple

from skippy_sandbox import ToolResult, cap_text

logger = logging.getLogger("skippy_rizin")

# The build ADR 0018 pins. Overridable by environment for a machine that keeps its
# tools elsewhere, but not by anything the model can reach.
DEFAULT_PREFIX = os.path.expanduser("~/skippy-tools/rizin-0.9.1")
PREFIX_ENV = "SKIPPY_RIZIN_PREFIX"

# By absolute path, deliberately. A Homebrew rizin earlier on PATH is built against
# system capstone and silently lacks Xtensa and RISC-V, so resolving by name could
# quietly cost two architectures with no error anywhere.
_BINARIES = ("rizin", "rz-bin", "rz-asm")

# A function is small. This cap exists for the pathological case — a 4000-instruction
# state machine — where the honest answer is that the whole thing does not belong in
# one observation.
MAX_FUNCTION_CHARS = 24_000

# Decompilation is slower than disassembly and neither should be able to hang a run.
DISASSEMBLE_TIMEOUT = 60.0
DECOMPILE_TIMEOUT = 180.0

# What a symbol may contain. Assembler and linker names across the formats we care
# about, and nothing else: no whitespace, no quotes, no shell or rizin metacharacter.
# This is not the control that keeps `!` out of a command string — resolution to an
# address is — but a symbol that cannot be a symbol is worth refusing on its own.
_SYMBOL_RE = re.compile(r"\A[A-Za-z_$.@][A-Za-z0-9_$.@:<>~*+-]{0,254}\Z")
_ADDRESS_RE = re.compile(r"\A0x[0-9a-fA-F]{1,16}\Z")

# rizin's arch name -> the Sleigh processor to name when decompiling. ADR 0018: these
# two are absent from rz-ghidra's ArchMap, so nothing selects them automatically, but
# Ghidra 12.1 ships both Sleigh specs and ArchMap has an explicit override.
_SLEIGH_CPU = {"xtensa": "Xtensa", "riscv": "RISCV"}

# Architectures whose calling convention rz-ghidra cannot match, so parameter lists in
# the output are unreliable. Passed through to the model rather than trimmed: an
# invented signature that reads as fact is exactly what ADR 0012's confidence field
# exists to prevent.
_UNRELIABLE_ARGS = {"xtensa": "call0", "riscv": "rvg"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# rizin reports these on stdout with a zero exit status, so the exit code cannot be
# trusted to say whether the question was answered. Treating "Cannot find function" as
# success would hand the model an observation labelled OK whose body is an error.
_FAILURE_MARKERS = (
    "Cannot find function",
    "Cannot seek",
    "Unknown asm plugin",
    "Ghidra Decompiler Error",
    "Could not find .sla file",
    "Invalid address",
)

# Diagnostics rizin prints while reading past the end of a mapped section. They are
# noise rather than failures — the disassembly or decompilation is still produced — but
# they are prefixed `ERROR:` and would read to the model as the tool having failed, and
# a dozen of them ahead of the answer is worse than none of them.
_BENIGN_NOISE = (
    "Incomplete buffer read",
    "Cannot read buffer at",
    "Warning: Invalid range",
)

_ARCH_CACHE: Optional[frozenset] = None
_VALID_BITS = (8, 16, 32, 64)


class RizinError(Exception):
    """rizin could not be run, or could not answer the question asked of it."""


class SymbolError(RizinError):
    """The symbol is not a symbol, or is not in this binary."""


def prefix() -> str:
    return os.environ.get(PREFIX_ENV) or DEFAULT_PREFIX


def tool_path(name: str) -> str:
    """The absolute path to one of the rizin binaries, or raise.

    Falls back to `PATH` only when the pinned prefix is absent, so that a machine with
    a system rizin is usable while the pinned build stays preferred. The fallback is
    reported in `describe()` because it changes which architectures exist.
    """
    if name not in _BINARIES:
        raise RizinError(f"Not a rizin binary: {name}")
    pinned = os.path.join(prefix(), "bin", name)
    if os.path.isfile(pinned) and os.access(pinned, os.X_OK):
        return pinned
    found = shutil.which(name)
    if found:
        return found
    raise RizinError(
        f"'{name}' is not installed. The pinned build belongs at {prefix()} — see "
        "ADR 0018; a Homebrew rizin will not do, because it is built against system "
        "capstone and has no Xtensa or RISC-V."
    )


def available() -> bool:
    try:
        tool_path("rizin")
        tool_path("rz-bin")
    except RizinError:
        return False
    return True


def _child_env() -> Dict[str, str]:
    """A minimal environment.

    Allowlisted rather than filtered, like `skippy_exec`'s, and for one extra reason
    here: rizin reads `RZ_*` variables that can add plugin directories and change
    configuration, so inheriting the parent environment wholesale would reintroduce
    the startup-script problem that `-N` exists to close.
    """
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    prefix_lib = os.path.join(prefix(), "lib")
    if os.path.isdir(prefix_lib):
        env["DYLD_LIBRARY_PATH"] = prefix_lib
        env["LD_LIBRARY_PATH"] = prefix_lib
    return env


def _clean(text: str) -> str:
    """Strip terminal control sequences, benign diagnostics, and blank edges.

    rizin writes progress indicators to stdout even non-interactively, and `[2K` in the
    middle of recorded evidence is noise a person reading the pack has to look past.

    The benign diagnostics go for a stronger reason than tidiness. They are prefixed
    `ERROR:` while the tool has in fact succeeded, so leaving them in front of a correct
    decompilation teaches the model to read past lines beginning with ERROR — which is
    the one habit that makes every real refusal in this system less effective.
    """
    kept = [
        line for line in _ANSI_RE.sub("", text).splitlines()
        if not any(noise in line for noise in _BENIGN_NOISE)
    ]
    return "\n".join(kept).strip("\n")


async def _run(argv: List[str], timeout: float) -> Tuple[int, str]:
    """Run one rizin binary and return (exit code, output).

    The only place in this module that starts a process, so the invariants live here:
    no shell, `-N` already present in argv, stdin closed, and a timeout that kills the
    process group rather than leaving a decompiler spinning.
    """
    if os.path.basename(argv[0]) == "rizin":
        if "-N" not in argv:
            raise RizinError("Refusing to run rizin without -N.")
        for forbidden in ("-w", "-W"):
            if forbidden in argv:
                raise RizinError(f"Refusing to run rizin with {forbidden}.")

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=_child_env(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RizinError(f"Could not start '{argv[0]}': {exc}") from exc
    except OSError as exc:
        raise RizinError(f"Could not start '{argv[0]}': {exc}") from exc

    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError):
            process.kill()
        raise RizinError(
            f"'{os.path.basename(argv[0])}' did not finish within {timeout:.0f}s."
        ) from None

    return process.returncode or 0, _clean((out or b"").decode("utf-8", "replace"))


async def _json(argv: List[str], timeout: float, key: str) -> Any:
    code, out = await _run(argv, timeout)
    if not out:
        raise RizinError(f"'{os.path.basename(argv[0])}' produced no output (exit {code}).")
    # rz-bin prefixes warnings to stdout, so find the JSON rather than assuming it
    # starts at character zero.
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise RizinError(f"'{os.path.basename(argv[0])}' did not return JSON: {out[:200]}")
    try:
        parsed = json.loads(out[start:])
    except json.JSONDecodeError as exc:
        raise RizinError(f"Could not read JSON from rz-bin: {exc}") from exc
    if isinstance(parsed, dict):
        return parsed.get(key, parsed)
    return parsed


async def known_archs() -> frozenset:
    """The architectures this rizin build can actually disassemble.

    Asked of the build rather than hard-coded, because the whole reason ADR 0018 gave
    up on the Homebrew bottle is that two architectures were missing from it. A list in
    this file would claim Xtensa on a build that does not have it, which is the exact
    failure mode the ADR exists to stop.
    """
    global _ARCH_CACHE
    if _ARCH_CACHE is not None:
        return _ARCH_CACHE
    try:
        _, out = await _run([tool_path("rz-asm"), "-L"], DISASSEMBLE_TIMEOUT)
    except RizinError:
        _ARCH_CACHE = frozenset()
        return _ARCH_CACHE
    names = set()
    for line in out.splitlines():
        # `_dAeI 16 32 64   xtensa   LGPL3   Tensilica Xtensa ...` — flags, then one or
        # more word sizes, then the name.
        parts = line.split()
        for part in parts[1:]:
            if part.isdigit():
                continue
            if re.fullmatch(r"[a-z0-9_.]+", part):
                names.add(part)
            break
    _ARCH_CACHE = frozenset(names)
    return _ARCH_CACHE


async def validate_arch(arch: str) -> str:
    """Check an architecture name against what this build supports.

    Model-supplied and interpolated into a rizin command, so it is checked against the
    build's own plugin list rather than a pattern. Nothing reaches the command string
    that rizin did not name first.
    """
    name = str(arch or "").strip().lower()
    if not name:
        return ""
    if not re.fullmatch(r"[a-z0-9_.]{1,24}", name):
        raise RizinError(f"'{arch}' is not an architecture name.")
    known = await known_archs()
    if known and name not in known:
        close = sorted(a for a in known if name[:3] in a)[:6]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise RizinError(
            f"This rizin build has no '{name}' disassembler.{hint} Ask for the "
            "architecture as rizin names it — arm, x86, mips, riscv, xtensa."
        )
    return name


def _validate_bits(bits: Any) -> Optional[int]:
    if bits in (None, "", 0):
        return None
    try:
        value = int(bits)
    except (TypeError, ValueError):
        raise RizinError(f"'{bits}' is not a word size.") from None
    if value not in _VALID_BITS:
        raise RizinError(f"Word size must be one of {_VALID_BITS}, not {value}.")
    return value


async def describe(target: str, arch: str = "", bits: Any = None) -> Dict[str, Any]:
    """What the binary is: format, architecture, word size, endianness.

    Read with `rz-bin`, which parses headers without analysing code, so this is cheap
    enough to do on every call and removes the need for the model to say what the
    architecture is — a thing it would sometimes get wrong and always have to guess.

    A raw flash dump has no headers to read, so there `arch` is the only way to know,
    and an ESP32 image is exactly that case. The override applies only where the
    container said nothing: a Mach-O that declares AArch64 is not up for discussion,
    and letting a supplied value win there would turn a typo into wrong disassembly
    rather than an error.
    """
    info = await _json([tool_path("rz-bin"), "-j", "-I", target], DISASSEMBLE_TIMEOUT, "info")
    if not isinstance(info, dict):
        raise RizinError("rz-bin returned no information about this file.")
    detected = str(info.get("arch") or "").lower()
    fmt = str(info.get("bintype") or info.get("class") or "").lower() or "unknown"
    raw = fmt in ("unknown", "any", "")

    supplied = await validate_arch(arch)
    supplied_bits = _validate_bits(bits)
    if supplied and detected and not raw and supplied != detected:
        raise RizinError(
            f"This file declares itself {detected}, so it will not be read as "
            f"{supplied}. Drop the arch argument, or check you have the right file."
        )

    final_arch = detected or supplied
    if raw and supplied:
        final_arch = supplied
    final_bits = int(info.get("bits") or 0) or None
    if raw and supplied_bits:
        final_bits = supplied_bits

    return {
        "arch": final_arch,
        "bits": final_bits,
        "endian": str(info.get("endian") or "").upper() or None,
        "format": fmt,
        "os": str(info.get("os") or "") or None,
        "stripped": bool(info.get("stripped")),
        "raw": raw,
        "arch_supplied": bool(raw and supplied),
        "sleigh_cpu": _SLEIGH_CPU.get(final_arch),
        "pinned_build": os.path.isfile(os.path.join(prefix(), "bin", "rizin")),
    }


def validate_symbol(symbol: str) -> str:
    """Refuse anything that is not a symbol name or a hex address.

    The refusal names what is allowed, because ADR 0012 established that a refusal
    which does not say how to succeed produces the same call again.
    """
    name = str(symbol or "").strip()
    if not name:
        raise SymbolError("A symbol name or 0x-prefixed address is required.")
    if _ADDRESS_RE.match(name) or _SYMBOL_RE.match(name):
        return name
    raise SymbolError(
        f"'{name[:60]}' is not a usable symbol. Give a symbol name as it appears in the "
        "binary, or a hex address like 0x100003f40 for a stripped target."
    )


async def symbols(target: str, limit: int = 0) -> List[Dict[str, Any]]:
    """Every named function-ish symbol, from the symbol table rather than analysis."""
    raw = await _json([tool_path("rz-bin"), "-j", "-s", target], DISASSEMBLE_TIMEOUT, "symbols")
    found: List[Dict[str, Any]] = []
    seen = set()
    for entry in raw if isinstance(raw, list) else []:
        name = str(entry.get("name") or "")
        vaddr = entry.get("vaddr")
        if not name or not isinstance(vaddr, int) or vaddr <= 0:
            continue
        key = (name, vaddr)
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "name": name,
            "realname": str(entry.get("realname") or name),
            "vaddr": vaddr,
            "type": str(entry.get("type") or ""),
        })
        if limit and len(found) >= limit:
            break
    return found


def _candidates(name: str) -> List[str]:
    """The names a model might reasonably give for one symbol.

    Mach-O prefixes C symbols with an underscore and rizin prefixes flags with `sym.`,
    so a model reading source and asking for `verify_image` is asking for the same
    function that appears as `_verify_image`. Accepting the obvious variants is
    cheaper than a refusal that teaches nothing.
    """
    stripped = name
    for prefix_ in ("sym.imp.", "sym.", "imp.", "func.", "fcn."):
        if stripped.startswith(prefix_):
            stripped = stripped[len(prefix_):]
            break
    forms = [name, stripped, f"_{stripped}", stripped.lstrip("_")]
    out = []
    for form in forms:
        if form and form not in out:
            out.append(form)
    return out


async def resolve(target: str, symbol: str) -> Tuple[int, str]:
    """Turn a model-supplied symbol into (address, resolved name).

    This is the security boundary as much as the convenience one. Once the answer is
    an integer, what reaches rizin's command language is a number this module
    formatted, and the model's string is out of the picture entirely.
    """
    name = validate_symbol(symbol)
    if _ADDRESS_RE.match(name):
        return int(name, 16), name

    table = await symbols(target)
    wanted = _candidates(name)
    by_name = {}
    for entry in table:
        by_name.setdefault(entry["name"], entry)
        by_name.setdefault(entry["realname"], entry)
    for form in wanted:
        hit = by_name.get(form)
        if hit:
            return hit["vaddr"], hit["name"]

    lowered = {key.lower(): value for key, value in by_name.items()}
    for form in wanted:
        hit = lowered.get(form.lower())
        if hit:
            return hit["vaddr"], hit["name"]

    raise SymbolError(_not_found_message(name, table))


def _not_found_message(name: str, table: List[Dict[str, Any]]) -> str:
    """Say what to do next, not just that it failed.

    A stripped binary and a mistyped name are different problems with different
    answers, and a bare "not found" leaves the model to guess which it hit.
    """
    if not table:
        return (
            f"'{name}' was not found, and this binary has no symbol table to search — "
            "it is probably stripped. Give a hex address instead; `run_command` with "
            "`objdump`, `strings` or a header dump is the way to find one."
        )
    needle = name.lower().lstrip("_")
    near = [
        entry["name"] for entry in table
        if needle and needle in entry["name"].lower()
    ][:8]
    if near:
        return f"'{name}' was not found. Similar symbols in this binary: {', '.join(near)}."
    sample = ", ".join(entry["name"] for entry in table[:8])
    return (
        f"'{name}' is not in this binary's {len(table)} symbols. Some of them: {sample}. "
        "A hex address also works."
    )


def _open_flags(info: Dict[str, Any]) -> List[str]:
    """Force architecture only where rizin cannot work it out itself.

    A recognised container carries its own architecture and overriding it does harm.
    A raw flash dump carries nothing, so an ESP32 image needs to be told — which is
    the case that matters most here.
    """
    if info["format"] not in ("unknown", "any", ""):
        return []
    flags = []
    if info["arch"]:
        flags += ["-a", info["arch"]]
    if info["bits"]:
        flags += ["-b", str(info["bits"])]
    return flags


def _script(address: int, info: Dict[str, Any], decompile_it: bool) -> str:
    """The rizin commands to run, as one `-c` string we own entirely.

    The two-stage architecture switch for Xtensa and RISC-V lives here. Analysis has
    to happen under the native Capstone plugin, because the Sleigh analysis plugin
    finds no functions on Xtensa and leaves nothing to decompile; the architecture is
    then switched for the decompile step alone. The model asks for a function and
    never learns that two architectures were involved.
    """
    arch = info["arch"]
    steps = ["e scr.color=0", "e scr.interactive=false"]
    if arch:
        steps.append(f"e analysis.arch={arch}")
    steps += [f"s {address:#x}", "af"]
    if not decompile_it:
        steps.append("pdf")
        return "; ".join(steps)

    cpu = info.get("sleigh_cpu")
    if cpu:
        steps += ["e asm.arch=ghidra", f"e asm.cpu={cpu}"]
        if info["bits"]:
            steps.append(f"e asm.bits={info['bits']}")
    steps.append("pdg")
    return "; ".join(steps)


async def _view(
    target: str, symbol: str, decompile_it: bool, arch: str = "", bits: Any = None
) -> Dict[str, Any]:
    info = await describe(target, arch=arch, bits=bits)
    address, resolved = await resolve(target, symbol)
    argv = [tool_path("rizin"), "-N", "-q", "-e", "bin.cache=false"]
    argv += _open_flags(info)
    argv += ["-c", _script(address, info, decompile_it), target]
    timeout = DECOMPILE_TIMEOUT if decompile_it else DISASSEMBLE_TIMEOUT
    code, out = await _run(argv, timeout)
    return {
        "address": address,
        "symbol": resolved,
        "info": info,
        "output": out,
        "exit_code": code,
        # Quoted, because this ends up in the pack as the line a person retypes to
        # check a finding. `" ".join` produces something that looks like a command and
        # is not one: the `-c` script would be read as separate shell words.
        "command": shlex.join(argv),
    }


def _caveats(info: Dict[str, Any], decompile_it: bool) -> List[str]:
    notes = []
    convention = _UNRELIABLE_ARGS.get(info["arch"])
    if decompile_it and convention:
        notes.append(
            f"rz-ghidra cannot match the {convention} calling convention on "
            f"{info['arch']}, so the parameter list above is unreliable even where the "
            "body is correct. Do not record a claim about this function's signature "
            "from this output alone."
        )
    if decompile_it:
        notes.append(
            "Decompiler output is a reconstruction, not source. A weakness resting on "
            "it alone is 'likely' at best."
        )
    if not info.get("pinned_build"):
        notes.append(
            "This is not the pinned rizin build, so Xtensa and RISC-V may be missing "
            "(see ADR 0018)."
        )
    return notes


def _failure(body: str) -> str:
    """The rizin error in this output, if the output is only an error."""
    for marker in _FAILURE_MARKERS:
        if marker in body:
            return next(
                (line.strip() for line in body.splitlines() if marker in line), marker
            )
    return ""


def _no_function_advice(info: Dict[str, Any]) -> str:
    """Why an address might hold no function, most likely cause first."""
    if info["raw"] and not info["arch_supplied"]:
        return (
            " This file has no recognisable container, so nothing said what "
            "architecture it is and the default is unlikely to be right. Pass arch — "
            "'xtensa' for an ESP32 image, 'arm', 'riscv', 'mips', 'x86'."
        )
    if info["raw"]:
        return (
            f" The file is being read as {info['arch']}, which may be wrong for this "
            "offset, or the offset may not be the start of a function."
        )
    return (
        " If the address came from a header or a string it may point at data rather "
        "than code."
    )


def _present(view: Dict[str, Any], decompile_it: bool) -> ToolResult:
    info = view["info"]
    what = "Decompiled" if decompile_it else "Disassembled"
    where = f"{view['symbol']} at {view['address']:#x}"
    arch = " ".join(str(part) for part in (info["arch"], info["bits"], info["endian"]) if part)
    body = view["output"]

    # rizin reports both of these on stdout and still exits zero, so neither the exit
    # code nor a non-empty body means the question was answered. An observation labelled
    # OK whose content is an error is worse than a refusal: the model reads the label.
    problem = _failure(body)
    if problem or not body:
        detail = problem or "no function could be built there"
        return ToolResult(
            False,
            f"Could not read {where}: {detail}.{_no_function_advice(info)}",
            data={"symbol": view["symbol"], "address": view["address"], "arch": info["arch"]},
        )

    caveats = _caveats(info, decompile_it)
    content = body
    if caveats:
        content += "\n\n" + "\n".join(f"NOTE: {line}" for line in caveats)
    return ToolResult(
        True,
        f"{what} {where} ({arch}, {info['format']}).",
        cap_text(content, MAX_FUNCTION_CHARS),
        data={
            "symbol": view["symbol"],
            "address": view["address"],
            "arch": info["arch"],
            "format": info["format"],
            "command": view["command"],
            "decompiled": decompile_it,
        },
    )


def _target_of(pack: Any) -> str:
    """The artifact this pack is about.

    Taken from the pack rather than accepted as an argument, for the same reason the
    sandbox and the mode are injected: a tool call that could name its own target
    could read a file the session was never pointed at, and the pack's whole identity
    is which artifact it concerns.
    """
    target = str((getattr(pack, "meta", None) or {}).get("target") or "")
    if not target:
        raise RizinError(
            "This session has no target artifact, so there is nothing to disassemble. "
            "A target is set when the run starts."
        )
    resolved = os.path.realpath(os.path.expanduser(target))
    if not os.path.isfile(resolved):
        raise RizinError(
            f"The target '{target}' is not readable now. It may have moved since the "
            "pack was created."
        )
    return resolved


async def _tool(
    pack: Any, symbol: str, decompile_it: bool, arch: str = "", bits: Any = None
) -> ToolResult:
    try:
        target = _target_of(pack)
    except RizinError as exc:
        return ToolResult(False, str(exc))
    if not available():
        return ToolResult(
            False,
            "rizin is not installed, so disassembly and decompilation are unavailable. "
            "The static tools in `run_command` still work; record what you can "
            "establish from those, and record a question for the rest.",
        )
    try:
        view = await _view(target, symbol, decompile_it, arch=arch, bits=bits)
    except SymbolError as exc:
        return ToolResult(False, str(exc))
    except RizinError as exc:
        return ToolResult(False, str(exc))
    return _present(view, decompile_it)


async def disassemble_function(
    pack: Any, symbol: str, arch: str = "", bits: Any = None
) -> ToolResult:
    """One function's instructions, with rizin's analysis of its arguments and locals."""
    return await _tool(pack, symbol, decompile_it=False, arch=arch, bits=bits)


async def decompile(pack: Any, symbol: str, arch: str = "", bits: Any = None) -> ToolResult:
    """One function as C, via the Ghidra decompiler."""
    return await _tool(pack, symbol, decompile_it=True, arch=arch, bits=bits)


async def list_symbols(pack: Any, contains: str = "") -> ToolResult:
    """The named functions in the target, optionally filtered.

    Here because the alternative is the model guessing symbol names, and a guess costs
    a failed call plus the tokens to explain it. Names only — no addresses beyond what
    is needed to ask the next question — because this is navigation, not evidence.
    """
    try:
        target = _target_of(pack)
    except RizinError as exc:
        return ToolResult(False, str(exc))
    if not available():
        return ToolResult(False, "rizin is not installed, so the symbol table is unavailable.")
    try:
        table = await symbols(target)
    except RizinError as exc:
        return ToolResult(False, str(exc))

    needle = str(contains or "").strip().lower()
    if needle:
        table = [entry for entry in table if needle in entry["name"].lower()]
    if not table:
        if needle:
            return ToolResult(True, f"No symbol in this binary contains '{needle}'.")
        return ToolResult(
            True,
            "This binary has no symbol table; it is probably stripped. Disassemble by "
            "hex address instead.",
        )
    lines = [f"{entry['vaddr']:#x}  {entry['name']}" for entry in table[:400]]
    more = "" if len(table) <= 400 else f" (showing 400 of {len(table)})"
    return ToolResult(
        True,
        f"{len(table)} symbol(s){more}.",
        "\n".join(lines),
        data={"count": len(table)},
    )
