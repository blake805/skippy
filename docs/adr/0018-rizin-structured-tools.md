# 0018 — rizin and rz-ghidra behind structured tools, never behind the allowlist

Status: accepted
Date: 2026-07-31
Closes the architecture and decompiler gaps admitted in
[ADR 0012](0012-reverse-engineering-mode.md) and restated in
[0015](0015-note-pack-identity.md)–[0017](0017-weakness-findings-and-handoff.md). Depends
on the pre-digestion argument in [ADR 0007](0007-model-roles-and-cloud-escalation.md).

## Context

ADR 0012 shipped RE mode with Apple's static tools. That covered three of the six
architectures this shop builds for and no decompiler at all, so an ESP32 image — the most
common thing we actually need to read — could not be disassembled, let alone decompiled.
Everything also went through raw `run_command`, so a request for one function's
disassembly returned a region sized by the tool rather than by the question, which is the
opposite of what ADR 0007 concluded makes the 480B affordable.

## Decision

rizin for disassembly and rz-ghidra for decompilation, reached **only** through structured
function-scoped tools. Not Ghidra headless: rz-ghidra is the Ghidra decompiler and Sleigh
disassembler as self-contained C++, so there is no JVM and no Ghidra install, and the same
process that has the binary open does the decompiling.

### rizin cannot go in `INSPECTION_RULES`, and this is not a close call

The entire value of the RE lane is that it cannot execute the artifact or anything else.
`rizin` breaks that in three independent ways, all verified on the build we installed:

```
rizin -q -c '!touch /tmp/pwned.txt' target      → file created
rizin -q -c 'i | sh -c "touch /tmp/pwned.txt"'  → file created
```

`!` runs a shell command and `|` pipes output to an arbitrary program. Either one, reachable
through a single allowlisted `run_command`, converts the inspection lane into a general
execution lane. No `forbidden_flags` set can fix this, because the payload is inside the
`-c` argument's own command language rather than in the flags.

The third is the one worth dwelling on, because it is the binwalk attack chain in our own
new tooling. rizin executes `~/.rizinrc` on startup, and that file may contain `!`:

```
echo '!touch /tmp/rc_pwned.txt' > ~/.rizinrc
rizin -q -c '?e ok' target      → file created
rizin -N -q -c '?e ok' target   → not created
```

So anything able to write one file into the home directory gets arbitrary code execution on
the next RE session. That is exactly why binwalk 2.x's unfixed path traversal is an RCE
rather than a nuisance: it auto-loads plugins from `~/.config/binwalk/plugins/`. We are
about to add an extractor that points format parsers at hostile blobs
([ADR 0019](0019-firmware-extraction.md)), so this is not hypothetical for us. **Every
rizin invocation passes `-N`**, and a test asserts it, because the flag is the whole defence.

Consequently rizin is invoked only by us, with an argument vector we build, and it is absent
from `INSPECTION_RULES` so the model cannot reach it through `run_command`.

### Function-scoped by construction

`disassemble_function(symbol)` and `decompile(symbol)` each return one function. This is the
same argument as ADR 0007's: the 480B is affordable because its context stays small and
pre-digested, and a tool that returns a tool-sized region rather than a question-sized one
spends that budget on padding. `pdf` and `pdg` are already function-scoped, so the shape we
want is the shape rizin natively produces.

Symbols are the interface rather than offsets, because a symbol is what the model has a
reason to hold and an offset is a thing to get wrong. Model-supplied symbols are validated
against a strict character set and resolved against the binary's actual symbol table before
they reach a command string. The validation is not defence in depth over the `-N` argument
above — it is the primary control on the one input the model does supply, since the command
language that `!` and `|` live in is the same language a symbol name gets interpolated into.

### What the six families actually get

Verified on this build, against real instruction bytes rather than from documentation:

| family | disassembly (`pdf`) | decompilation (`pdg`) |
| --- | --- | --- |
| x86-64 | yes, Zydis | yes, automatic |
| ARM Cortex-M (thumb) | yes, Capstone | yes, automatic |
| ARM/AArch64 Linux | yes, Capstone | yes, automatic |
| MIPS | yes, Capstone | yes, automatic |
| RISC-V | yes, Capstone | yes, after a build fix and explicit selection |
| Xtensa (ESP32) | yes, Capstone, with IL lifting | yes, after explicit selection |

Three things had to be established to get there, and each is a constraint worth recording
rather than a step that happened to work.

**The Homebrew bottle cannot provide Xtensa or RISC-V.** rizin gates those two plugins on
the capstone version — Xtensa on `next` specifically, RISC-V on `next` or major above 6 —
and Homebrew builds against system capstone 5 with `--wrap-mode=nodownload`, so both compile
out silently. A future Homebrew capstone 6 still satisfies neither gate. We therefore build
rizin from source with its bundled capstone subproject, which is pinned to a commit rather
than tracking a branch. This is the cost of the decision: we own a build, and `brew upgrade`
is not the update path. Recorded versions: rizin v0.9.1 at `c3a90e9`, capstone-next at
`3df6ff01`, rz-ghidra v0.9.0 with Ghidra 12.1 at `3e5d774`.

**Xtensa and RISC-V decompile, but only when told to.** An earlier reading of
rz-ghidra's `ArchMap.cpp` found no `xtensa` or `riscv` entry and concluded decompilation was
unavailable for both. That conclusion was wrong, and the correction matters more than the
error: Ghidra 12.1 ships `Xtensa:LE:32:default` and `RISCV:LE:64:default` Sleigh specs, and
`ArchMap` has an explicit override — `asm.arch=ghidra` with `asm.cpu` naming a Sleigh
processor, or a full language id when it contains a colon. What `ArchMap` lacks is only the
*automatic* mapping from rizin's arch name.

The Sleigh analysis plugin will not build functions for Xtensa, so a single-arch run finds
nothing to decompile. The working sequence analyses with the native Capstone plugin and then
switches architecture for the decompile step alone:

```
rizin -N -b 32 -a xtensa -c 'e analysis.arch=xtensa; af; e asm.arch=ghidra; e asm.cpu=Xtensa; pdg'
```

That produced `return arg2 + 0x2a;` for a hand-encoded `a3 + 42`, in both the call0 and the
windowed ABI that ESP32 application code uses. The tool performs this switch itself; the
model asks for a function and does not learn that two architectures were involved.

**RISC-V decompilation needed a one-line fix to rz-ghidra's build.** Its CMake computes each
Sleigh output name with `NAME_WE`, which strips from the *first* dot, so
`riscv.lp64d.slaspec` and `riscv.ilp32d.slaspec` both compiled to `riscv.sla` — one
clobbering the other, and neither matching the `riscv.lp64d.sla` that `riscv.ldefs`
references. The failure surfaced as `Could not find .sla file for RISCV:LE:64:default`.
`NAME_WLE` fixes it. RISC-V is the only affected architecture, because it is the only one
whose spec filenames contain a dot. The patch is carried locally and should go upstream.

### Limits, stated before anyone relies on the table above

**Parameter lists are unreliable on Xtensa and RISC-V.** Both emit
`Matching calling convention ... failed, args may be inaccurate` — `call0` and `rvg`
respectively. Function bodies decompiled correctly in every case tested; the signatures did
not. The warning is passed through to the model rather than stripped, because a decompiled
signature that silently invented its parameters is precisely the kind of plausible-looking
detail ADR 0012's confidence field exists to stop from hardening into fact.

**Decompiler output is a reading aid, not ground truth.** It is a machine's reconstruction,
and a `weakness` recorded from it alone is `likely` at best. The prompt says so.

**Neither tool is a substitute for running the thing.** Dynamic analysis still needs the VM
boundary, unchanged from ADR 0012.

## Does the RE lane still need the 480B?

ADR 0007 chose the 480B for the planner role on tool discipline, and ADR 0013 recorded a
30B run that made five identical malformed `read_file` calls in one turn and invented a
`ggrep` tool. This ADR changes the premise that choice was made under: the model no
longer reads tool-sized regions, it reads one function at a time. So the question is
open again, and it is measurable rather than arguable.

The target is `benchmarks/updater.c` compiled with no source in the workspace: a firmware
update path that checks a magic number and a CRC32 and has no signature, with a
provisioning key left unused in the binary. The answers are known in advance, so a run
can be scored instead of admired. Same task, same tools, same 30-step budget.

| | 30B (`fast`) | 480B (`heavy`) |
| --- | --- | --- |
| outcome | finished, 27 steps | finished, 22 steps |
| tool calls | 27 | 21 |
| findings | 2 | 6 |
| the weakness | found, `high` / `likely` | found, `critical` / `confirmed` |
| wall clock | 43s | 135s |

**Both models found the weakness, which is new.** Before this slice the 30B could not
have: it would have been reading `objdump` regions. Pre-digested, function-scoped output
closed most of the gap, and that is the honest headline.

**The 480B is kept anyway, for two reasons that are about judgment rather than capacity.**

It graded the weakness correctly. `critical` / `confirmed` against the 30B's `high` /
`likely`, on a finding where the decompiled body shows the entire validation and there is
nothing left to confirm. Under-grading is the specific error this shop cannot absorb,
because ADR 0017 made severity the order in which work reaches a coding session — a
critical recorded as high is a fix that waits behind something that matters less.

It recorded three times as much. The 480B wrote down the header layout, the 1 MB size
limit, the magic number and a hypothesis that `0x4b504c31` is ASCII; the 30B recorded the
CRC mechanism and the weakness and stopped. The extra findings are the structural ones,
which are exactly what next month's session opens the pack for. A run that finds the
headline and records none of the structure has to be redone to answer the next question.

**One more observation, which is the strongest argument against the 30B for this lane.**
An earlier pass ran the same task in a workspace that also held the C source. The 480B
used it and cited source lines — a flaw in the experiment, corrected by removing the
source — but the 30B made **141 tool calls in 16 steps and recorded nothing at all**.
Same model, same tools, one busier directory. Discipline that degrades when the
environment gets more complicated is not discipline, and firmware work does not happen in
a directory with one file in it.

So: 480B for the RE planner role, unchanged. But the 30B is now viable for a triage pass
in a way it was not before, and that is worth knowing — if a large image needs a first
sweep before a person decides what deserves the expensive model, the cheap one can now
drive these tools well enough to do it.

Two honesty notes about the measurement. This is three runs of the 480B and three of the
30B on one target, not a benchmark; run-to-run variance was large enough to see (the two
30B runs differed by two orders of magnitude in tool calls). And the target was built to
have findable answers, which flatters both models compared with a real image.

## Consequences

Six of six architectures can be disassembled and decompiled, against three of six and no
decompiler before. An ESP32 image is readable for the first time.

The model's context holds one function per request instead of a tool-sized region, which is
what ADR 0007 asked for and what ADR 0012 failed to deliver.

The RE lane's no-execute guarantee now depends on a flag (`-N`) and on symbol validation
rather than only on an allowlist table, so both are tested directly. The escapes above are
in the test suite as assertions about what our tools do *not* let through.

`rizin` remains installed at a versioned prefix and absent from `PATH` decisions: the tools
invoke it by absolute path, so a second rizin elsewhere on `PATH` — a Homebrew one, say —
cannot be picked up by accident and quietly remove two architectures.
