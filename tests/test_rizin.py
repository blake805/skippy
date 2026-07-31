"""Function-scoped disassembly and decompilation over rizin.

Two things are being tested here, and only one of them is about disassembly.

The first is containment. rizin's `-c` argument is a command language with a shell
escape in it, so unlike every other tool in the RE lane its safety cannot come from an
allowlist of flags. It comes from three things instead — `-N` on every invocation, no
`-w`, and resolving the model's symbol to an address before anything is interpolated —
and a property that rests on three implementation details rather than a table is one
that has to be asserted rather than assumed. See ADR 0018.

The second is that a refusal has to be followable. A raw ESP32 dump with no
architecture given cannot be disassembled, and the useful behaviour is to say which
argument is missing rather than to return an empty result or, worse, confident
disassembly of the wrong instruction set.

Tests that need rizin itself are marked and skipped where it is absent, so CI without
the pinned build still checks everything that does not need a subprocess.
"""

import asyncio
import os

import pytest

import skippy_re
import skippy_rizin
from skippy_rizin import RizinError, SymbolError

needs_rizin = pytest.mark.skipif(
    not skippy_rizin.available(), reason="the pinned rizin build is not installed"
)


@pytest.fixture
def notes_dir(tmp_path):
    return str(tmp_path / "notes")


@pytest.fixture
def elf_like(tmp_path):
    """A file that exists but is not a binary, for the paths that stop before rizin."""
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"\x00not really code\x00" + b"\xff" * 64)
    return str(path)


@pytest.fixture
def pack(notes_dir, elf_like):
    return skippy_re.open_pack(notes_dir, target=elf_like, title="probe")


# --- the model's symbol must never reach rizin's command language ---
#
# `!cmd` runs a shell command and `| prog` pipes to one, so a symbol that carried
# either into a `-c` string would turn the inspection lane into an execution lane.
# Nothing below runs a payload: the point is that the payload is refused, and that the
# command string is built from an integer this module formatted.

INJECTIONS = [
    "verify_image; !id",
    "!id",
    "verify_image | wc -l",
    "0x1000; wx ffffffff",
    "`id`",
    "$(id)",
    "verify_image\n!id",
    "verify_image && id",
    "'; wx ffff; '",
    "verify image",
    'sym."quoted"',
    "sym.foo#comment",
    "verify_image\x00truncated",
]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_a_symbol_that_is_not_a_symbol_is_refused(payload):
    with pytest.raises(SymbolError):
        skippy_rizin.validate_symbol(payload)


def test_the_refusal_says_what_would_work():
    """ADR 0012's lesson: a refusal that does not name a way out gets the same call
    back. Here the way out is a plain name or an address."""
    with pytest.raises(SymbolError) as caught:
        skippy_rizin.validate_symbol("verify_image; !id")
    message = str(caught.value)
    assert "symbol name" in message
    assert "0x" in message


@pytest.mark.parametrize("symbol", [
    "verify_image", "_verify_image", "sym._verify_image", "main",
    "_ZN3foo3barEv", "obj.g_state", "std::vector<int>::push_back",
    "0x100003f40", "0xdeadbeef",
])
def test_real_symbol_shapes_are_accepted(symbol):
    """The strictness has to stop at names that actually occur. C++ mangling, Mach-O
    underscores, rizin's own flag prefixes and demangled names with angle brackets all
    turn up in real symbol tables."""
    assert skippy_rizin.validate_symbol(symbol) == symbol


def test_only_an_address_and_our_own_words_reach_the_command_string():
    """The security property stated positively: whatever the model asked for, the `-c`
    string is built from a hex integer and a fixed set of rizin commands."""
    info = {
        "arch": "xtensa", "bits": 32, "endian": "LE", "format": "unknown",
        "raw": True, "arch_supplied": True, "sleigh_cpu": "Xtensa",
    }
    script = skippy_rizin._script(0x400d1234, info, decompile_it=True)
    assert "0x400d1234" in script
    for metacharacter in ("!", "|", "`", "$(", "\n", '"', "'"):
        assert metacharacter not in script


def test_an_architecture_is_checked_against_the_build_not_a_pattern():
    """An arch name does reach the command string, so it is checked against the
    architectures rizin reports rather than against a regex. A list in the module would
    claim Xtensa on a build that lacks it — the exact overclaiming ADR 0018 exists to
    stop."""
    with pytest.raises(RizinError):
        asyncio.run(skippy_rizin.validate_arch("xtensa; !id"))
    with pytest.raises(RizinError):
        asyncio.run(skippy_rizin.validate_arch("arm|sh"))


@pytest.mark.parametrize("bits", ["not a number", 7, 128, -32, 0.5])
def test_a_word_size_that_is_not_a_word_size_is_refused(bits):
    if bits in (0,):
        pytest.skip("zero means 'unset'")
    with pytest.raises(RizinError):
        skippy_rizin._validate_bits(bits)


def test_rizin_is_never_run_without_the_flag_that_blocks_startup_scripts():
    """rizin executes ~/.rizinrc, and that file may contain `!`. So anything able to
    write one file into the home directory would get code execution on the next RE
    session — the binwalk plugin-directory chain, in our own tooling. `-N` is the whole
    defence, which is why it is enforced at the one place that starts a process rather
    than trusted to each caller."""
    with pytest.raises(RizinError, match="-N"):
        asyncio.run(skippy_rizin._run(["/usr/bin/rizin", "-q", "-c", "i", "x"], 1.0))


@pytest.mark.parametrize("flag", ["-w", "-W"])
def test_rizin_is_never_run_in_write_mode(flag):
    """The RE lane's guarantee is that the artifact is not modified. `-w` is how rizin
    opens a file for writing, so it is refused at the same choke point."""
    with pytest.raises(RizinError, match=flag):
        asyncio.run(skippy_rizin._run(["/usr/bin/rizin", "-N", flag, "x"], 1.0))


def test_the_environment_handed_to_rizin_is_built_not_inherited():
    """RZ_* variables can add plugin directories and change configuration, so an
    inherited environment would reopen the hole `-N` closes."""
    env = skippy_rizin._child_env()
    assert not [name for name in env if name.startswith("RZ")]
    assert env["PATH"] == "/usr/bin:/bin"


def test_the_binaries_are_named_by_absolute_path():
    """A Homebrew rizin earlier on PATH is built against system capstone and silently
    has no Xtensa or RISC-V, so resolving by name could cost two architectures with no
    error anywhere."""
    assert os.path.isabs(skippy_rizin.tool_path("rizin"))


def test_only_the_rizin_binaries_can_be_asked_for():
    with pytest.raises(RizinError):
        skippy_rizin.tool_path("sh")


# --- the target comes from the pack, never from the call ---

def test_a_tool_call_cannot_name_its_own_target(pack, tmp_path):
    """Same reasoning as the injected sandbox and mode: a call that could choose its
    target could read a file the session was never pointed at, and the pack's identity
    is which artifact it concerns."""
    import inspect
    for tool in (skippy_rizin.disassemble_function, skippy_rizin.decompile,
                 skippy_rizin.list_symbols):
        assert "target" not in inspect.signature(tool).parameters


def test_a_session_with_no_target_says_so_rather_than_guessing(notes_dir):
    packless = skippy_re.open_pack(notes_dir, target="", title="no target")
    result = asyncio.run(skippy_rizin.disassemble_function(packless, "main"))
    assert not result.ok
    assert "no target" in result.summary.lower()


def test_a_target_that_has_gone_away_is_reported_as_that(notes_dir, tmp_path):
    """Distinguished from every other failure because the answer is different: the file
    moved, and no amount of rephrasing the symbol will help."""
    target = tmp_path / "gone.bin"
    target.write_bytes(b"\x00\x01\x02\x03")
    pack = skippy_re.open_pack(notes_dir, target=str(target), title="gone")
    target.unlink()
    result = asyncio.run(skippy_rizin.disassemble_function(pack, "main"))
    assert not result.ok
    assert "not readable" in result.summary


# --- symbols resolve the way a model would expect ---

@pytest.mark.parametrize("asked,expected", [
    ("verify_image", "_verify_image"),
    ("_verify_image", "_verify_image"),
    ("sym._verify_image", "_verify_image"),
    ("sym.imp.printf", "printf"),
])
def test_the_forms_a_model_might_use_all_name_the_same_function(asked, expected):
    """A model reading C source asks for `verify_image`; Mach-O calls it
    `_verify_image` and rizin flags it `sym._verify_image`. Refusing the difference
    would be a refusal that teaches nothing."""
    assert expected in skippy_rizin._candidates(asked)


# --- rizin's errors are failures, whatever its exit status says ---

@pytest.mark.parametrize("body", [
    "ERROR: Cannot find function at 0x00000000",
    "Ghidra Decompiler Error: No function at this offset",
    "ERROR: Could not find .sla file for RISCV:LE:64:default",
    "rz-asm: Unknown asm plugin 'xtensa'",
])
def test_an_error_on_stdout_is_not_a_successful_observation(body):
    """rizin prints these and exits zero. An observation labelled OK whose content is
    an error is worse than a refusal, because the model reads the label."""
    assert skippy_rizin._failure(body)


def test_a_real_disassembly_is_not_mistaken_for_an_error():
    body = "/ fcn.0000 (int32_t arg2);\n| 0x00 entry a1, 0x20\n\\ 0x08 retw.n"
    assert not skippy_rizin._failure(body)


def test_a_raw_dump_with_no_architecture_says_which_argument_is_missing():
    """The ESP32 case. Nothing in a flash dump declares the instruction set, and the
    failure a model can act on names the argument and gives it a value to try."""
    info = {"arch": "", "bits": None, "endian": "LE", "format": "unknown",
            "raw": True, "arch_supplied": False, "sleigh_cpu": None}
    advice = skippy_rizin._no_function_advice(info)
    assert "arch" in advice
    assert "xtensa" in advice


def test_a_container_that_declares_its_architecture_gets_different_advice():
    """A Mach-O that says AArch64 and has no function at an address is a different
    problem — most likely the address points at data — and identical advice about the
    arch argument would send the model the wrong way."""
    info = {"arch": "arm", "bits": 64, "endian": "LE", "format": "mach0",
            "raw": False, "arch_supplied": False, "sleigh_cpu": None}
    advice = skippy_rizin._no_function_advice(info)
    assert "arch" not in advice
    assert "data" in advice


# --- the caveats travel with the output ---

def test_an_unreliable_signature_is_flagged_where_it_is_unreliable():
    """rz-ghidra cannot match Xtensa's call0 or RISC-V's rvg calling convention, so
    parameter lists are invented while bodies are correct. Passed through rather than
    trimmed: a plausible signature reading as fact is what ADR 0012's confidence field
    exists to stop."""
    for arch, convention in (("xtensa", "call0"), ("riscv", "rvg")):
        info = {"arch": arch, "bits": 32, "format": "unknown", "raw": True,
                "arch_supplied": True, "sleigh_cpu": "Xtensa", "pinned_build": True}
        notes = " ".join(skippy_rizin._caveats(info, decompile_it=True))
        assert convention in notes
        assert "signature" in notes


def test_disassembly_carries_no_signature_caveat():
    """The warning belongs to the decompiler's argument recovery. On disassembly there
    is no reconstructed signature to be wrong, and a caveat that always fires is one
    the model learns to skip."""
    info = {"arch": "xtensa", "bits": 32, "format": "unknown", "raw": True,
            "arch_supplied": True, "sleigh_cpu": "Xtensa", "pinned_build": True}
    assert skippy_rizin._caveats(info, decompile_it=False) == []


def test_decompiled_output_always_says_it_is_a_reconstruction():
    info = {"arch": "arm", "bits": 64, "format": "mach0", "raw": False,
            "arch_supplied": False, "sleigh_cpu": None, "pinned_build": True}
    notes = " ".join(skippy_rizin._caveats(info, decompile_it=True))
    assert "reconstruction" in notes
    assert "likely" in notes


def test_an_unpinned_build_says_so_because_it_changes_what_exists():
    info = {"arch": "arm", "bits": 64, "format": "mach0", "raw": False,
            "arch_supplied": False, "sleigh_cpu": None, "pinned_build": False}
    notes = " ".join(skippy_rizin._caveats(info, decompile_it=False))
    assert "Xtensa" in notes and "RISC-V" in notes


# --- the two-stage architecture switch, which the model never sees ---

def test_xtensa_decompilation_switches_architecture_for_the_decompile_step_only():
    """The Sleigh analysis plugin finds no functions on Xtensa, so analysis has to
    happen under the native Capstone plugin and the architecture is switched afterwards
    for `pdg` alone. Order is the whole point: switching before `af` produces nothing
    to decompile."""
    info = {"arch": "xtensa", "bits": 32, "endian": "LE", "format": "unknown",
            "raw": True, "arch_supplied": True, "sleigh_cpu": "Xtensa"}
    script = skippy_rizin._script(0x400d0000, info, decompile_it=True)
    assert script.index("analysis.arch=xtensa") < script.index("af")
    assert script.index("af") < script.index("asm.arch=ghidra")
    assert script.index("asm.cpu=Xtensa") < script.index("pdg")


def test_an_architecture_ghidra_maps_itself_is_left_alone():
    """ArchMap resolves arm, x86 and mips automatically. Forcing `asm.arch=ghidra`
    there would replace a working automatic mapping with a manual one for no gain."""
    info = {"arch": "arm", "bits": 64, "endian": "LE", "format": "mach0",
            "raw": False, "arch_supplied": False, "sleigh_cpu": None}
    script = skippy_rizin._script(0x100000460, info, decompile_it=True)
    assert "asm.arch=ghidra" not in script
    assert script.endswith("pdg")


def test_analysis_is_scoped_to_the_one_function():
    """`aa` over a whole firmware image is the difference between a second and a coffee
    break, and produces nothing extra for a question about one function."""
    info = {"arch": "arm", "bits": 64, "endian": "LE", "format": "mach0",
            "raw": False, "arch_supplied": False, "sleigh_cpu": None}
    script = skippy_rizin._script(0x100000460, info, decompile_it=False)
    assert "; aa" not in script and not script.startswith("aa")
    assert "af" in script


def test_a_declared_architecture_is_not_forced_on_the_open():
    """Overriding the architecture of a container that declares one turns a good file
    into garbage, so `-a` is passed only where nothing declared anything."""
    declared = {"arch": "arm", "bits": 64, "format": "mach0", "raw": False,
                "arch_supplied": False, "sleigh_cpu": None}
    assert skippy_rizin._open_flags(declared) == []
    raw = {"arch": "xtensa", "bits": 32, "format": "unknown", "raw": True,
           "arch_supplied": True, "sleigh_cpu": "Xtensa"}
    assert skippy_rizin._open_flags(raw) == ["-a", "xtensa", "-b", "32"]


# --- against the real thing ---

@pytest.fixture
def compiled(tmp_path):
    """A binary with a function worth reading, built here rather than committed.

    A checked-in binary is opaque in review and goes stale against the toolchain; this
    is three lines of C whose decompilation has a constant in it we can look for.
    """
    import shutil
    import subprocess
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler")
    source = tmp_path / "sample.c"
    source.write_text(
        "#include <stddef.h>\n"
        "int verify_image(const unsigned char *b, size_t n) {\n"
        "    unsigned crc = 0;\n"
        "    for (size_t i = 0; i < n; i++) crc = (crc << 1) ^ b[i];\n"
        "    return crc == 0xDEADBEEF;\n"
        "}\n"
        "int main(void) { unsigned char b[2] = {1, 2}; return verify_image(b, 2); }\n"
    )
    binary = tmp_path / "sample"
    result = subprocess.run([cc, "-O1", str(source), "-o", str(binary)],
                            capture_output=True)
    if result.returncode != 0:
        pytest.skip(f"could not compile: {result.stderr[:200]!r}")
    return str(binary)


@needs_rizin
def test_a_real_function_disassembles_by_name(notes_dir, compiled):
    pack = skippy_re.open_pack(notes_dir, target=compiled, title="sample")
    result = asyncio.run(skippy_rizin.disassemble_function(pack, "verify_image"))
    assert result.ok, result.summary
    # The address is in the summary so a finding can cite it without a second call.
    assert "0x" in result.summary
    assert "verify_image" in result.summary


@needs_rizin
def test_a_real_function_decompiles_to_c_with_its_constants(notes_dir, compiled):
    """The constant is the check that this is the actual function and not a plausible
    shape: 0xdeadbeef was in the source and has to survive to the output."""
    pack = skippy_re.open_pack(notes_dir, target=compiled, title="sample")
    result = asyncio.run(skippy_rizin.decompile(pack, "verify_image"))
    assert result.ok, result.summary
    assert "deadbeef" in result.content.lower()
    assert "reconstruction" in result.content


@needs_rizin
def test_one_function_arrives_not_the_whole_binary(notes_dir, compiled):
    """The ADR 0007 property, measured rather than asserted: a function-scoped request
    returns a function-scoped answer."""
    pack = skippy_re.open_pack(notes_dir, target=compiled, title="sample")
    result = asyncio.run(skippy_rizin.disassemble_function(pack, "verify_image"))
    assert result.ok
    assert len(result.content) < 4000
    assert "main" not in result.content


@needs_rizin
def test_a_symbol_that_is_absent_lists_what_is_there(notes_dir, compiled):
    """A near-miss list turns a wrong guess into one more call rather than a dead end."""
    pack = skippy_re.open_pack(notes_dir, target=compiled, title="sample")
    result = asyncio.run(skippy_rizin.disassemble_function(pack, "verify_signature"))
    assert not result.ok
    assert "verify_image" in result.summary


@needs_rizin
def test_a_target_with_no_symbol_table_says_to_use_an_address(notes_dir, tmp_path):
    """The stripped case, and where a bare "not found" is least helpful: there is
    nothing to search, so the answer is a different kind of argument rather than a
    better-spelled symbol. A raw dump is used rather than a stripped binary because it
    is the same condition arrived at deterministically — Mach-O `strip -x` removes local
    symbols and leaves globals, so it does not reliably produce this state.
    """
    blob = tmp_path / "dump.bin"
    blob.write_bytes(bytes.fromhex("364100" "22a02a" "2a23" "1df0"))
    pack = skippy_re.open_pack(notes_dir, target=str(blob), title="dump")
    result = asyncio.run(
        skippy_rizin.disassemble_function(pack, "verify_image", arch="xtensa", bits=32)
    )
    assert not result.ok
    assert "stripped" in result.summary
    assert "hex address" in result.summary


@needs_rizin
def test_a_stripped_binary_can_still_be_read_by_address(notes_dir, tmp_path, compiled):
    """The other half of the same story: losing the symbols costs the name, not the
    ability to read the code, which is why the refusal above points at an address."""
    import shutil
    import subprocess
    stripper = shutil.which("strip")
    if not stripper:
        pytest.skip("no strip")
    stripped = str(tmp_path / "stripped")
    shutil.copy(compiled, stripped)
    subprocess.run([stripper, "-x", stripped], capture_output=True)

    # The address comes from somewhere else, which is exactly the real workflow: a
    # header, a string, a cross-reference, or in this case the build before stripping.
    before = skippy_re.open_pack(notes_dir, target=compiled, title="before")
    listed = asyncio.run(skippy_rizin.list_symbols(before, contains="verify"))
    assert listed.ok
    address = listed.content.split()[0]

    pack = skippy_re.open_pack(notes_dir, target=stripped, title="stripped")
    result = asyncio.run(skippy_rizin.disassemble_function(pack, address))
    assert result.ok, result.summary
    assert "0x" in result.summary


@needs_rizin
def test_the_six_architectures_this_shop_builds_for_are_all_present(notes_dir):
    """ADR 0018's central claim, checked against the build rather than the ADR. The six
    families are covered by five rizin plugins, because `arm` handles Cortex-M thumb and
    AArch64 both."""
    archs = asyncio.run(skippy_rizin.known_archs())
    for required in ("x86", "arm", "mips", "riscv", "xtensa"):
        assert required in archs, f"{required} missing: this is not the pinned build"


@needs_rizin
def test_an_esp32_shaped_raw_dump_disassembles_when_told_the_architecture(notes_dir, tmp_path):
    """Xtensa through the whole path, on bytes rather than on trust. These encodings are
    from Ghidra's own Xtensa function-start patterns: a windowed-ABI prologue, a
    constant load, an add and a windowed return.
    """
    blob = tmp_path / "firmware.bin"
    blob.write_bytes(bytes.fromhex("364100" "22a02a" "2a23" "1df0"))
    pack = skippy_re.open_pack(notes_dir, target=str(blob), title="esp32")

    result = asyncio.run(
        skippy_rizin.disassemble_function(pack, "0x0", arch="xtensa", bits=32)
    )
    assert result.ok, result.summary
    assert "entry" in result.content and "retw.n" in result.content


@needs_rizin
def test_the_same_dump_without_an_architecture_refuses_rather_than_inventing_one(
    notes_dir, tmp_path
):
    """The failure that matters most: confident disassembly of the wrong instruction set
    would be recorded as a finding."""
    blob = tmp_path / "firmware.bin"
    blob.write_bytes(bytes.fromhex("364100" "22a02a" "2a23" "1df0"))
    pack = skippy_re.open_pack(notes_dir, target=str(blob), title="esp32")
    result = asyncio.run(skippy_rizin.disassemble_function(pack, "0x0"))
    assert not result.ok
    assert "arch" in result.summary


@needs_rizin
def test_xtensa_decompiles_and_admits_the_signature_may_be_wrong(notes_dir, tmp_path):
    """The correction to the earlier conclusion that Xtensa could not be decompiled,
    pinned as a test so it cannot quietly regress — and the caveat pinned with it, since
    the body being right while the parameters are invented is the actual behaviour."""
    blob = tmp_path / "firmware.bin"
    blob.write_bytes(bytes.fromhex("364100" "22a02a" "2a23" "1df0"))
    pack = skippy_re.open_pack(notes_dir, target=str(blob), title="esp32")
    result = asyncio.run(skippy_rizin.decompile(pack, "0x0", arch="xtensa", bits=32))
    assert result.ok, result.summary
    assert "0x2a" in result.content
    assert "call0" in result.content


@needs_rizin
def test_riscv_decompiles(notes_dir, tmp_path):
    """Needed a one-line fix to rz-ghidra's CMake — `NAME_WE` collapsed
    `riscv.lp64d.slaspec` and `riscv.ilp32d.slaspec` onto one output — so this asserts
    the patched build is the one installed."""
    blob = tmp_path / "rv.bin"
    blob.write_bytes(bytes.fromhex("130101ff" "1305a502" "13010101" "67800000"))
    pack = skippy_re.open_pack(notes_dir, target=str(blob), title="riscv")
    result = asyncio.run(skippy_rizin.decompile(pack, "0x0", arch="riscv", bits=64))
    assert result.ok, result.summary
    assert "0x2a" in result.content


@needs_rizin
def test_a_declared_architecture_wins_over_a_supplied_one(notes_dir, compiled):
    """A file that says what it is does not get overridden: a typo would otherwise
    produce wrong disassembly rather than an error, and wrong disassembly gets
    recorded."""
    pack = skippy_re.open_pack(notes_dir, target=compiled, title="sample")
    result = asyncio.run(
        skippy_rizin.disassemble_function(pack, "verify_image", arch="mips")
    )
    assert not result.ok
    assert "declares itself" in result.summary


@needs_rizin
def test_symbols_can_be_listed_and_filtered(notes_dir, compiled):
    pack = skippy_re.open_pack(notes_dir, target=compiled, title="sample")
    everything = asyncio.run(skippy_rizin.list_symbols(pack))
    assert everything.ok
    assert "verify_image" in everything.content

    filtered = asyncio.run(skippy_rizin.list_symbols(pack, contains="verify"))
    assert filtered.ok
    assert "verify_image" in filtered.content
    assert "main" not in filtered.content

    empty = asyncio.run(skippy_rizin.list_symbols(pack, contains="nothingmatchesthis"))
    assert empty.ok  # a true answer, not a failure
    assert "No symbol" in empty.summary
