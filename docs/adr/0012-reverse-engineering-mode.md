# 0012 — Reverse-engineering mode: notes as the deliverable

Status: accepted
Date: 2026-07-29
Supersedes nothing. Follows 0010 (agent loop) and 0011 (command execution).

## Context

Reverse engineering was in scope from the start, and until now it has been handled by
pointing the coding agent at a binary. That works badly for two structural reasons,
and neither is fixed by prompting.

**The product is different.** A coding task ends in a diff, and the repository
remembers it: the work survives in a form that outlives the session by default. An RE
task changes nothing. Everything established lives in the transcript, which ADR 0010
folds once it passes 60k characters — so the earlier half of a long investigation is
compressed into prose and the details go. The agent finishes a session having learned
a great deal and recorded none of it.

**The tools are different in one specific and dangerous way.** The coding toolset is
built to execute: run the tests, run the linter, check the change. In RE, the code in
question is someone else's artifact, and running it is precisely what must not happen
by accident. The whole coding prompt — "after changing code, run the tests" — points
the wrong way.

## Decision

A second mode, chosen by the caller, that changes three things: the system prompt, the
toolset, and which command allowlist applies.

### Findings are files, and evidence is mandatory

Each finding is one markdown file with YAML front matter under `notes_root()`, which
lives beside the patch journal in memory rather than inside any workspace root. Plain
files, not a Chroma collection: the notes are frequently the only product of the work,
so they must survive an unmounted NAS, be readable by a person with no Skippy running,
diff sensibly, and be greppable. Search over them is a layer that can be added later.
Making search the storage would mean an unavailable vector store loses the work.

`note_finding` refuses a finding with no evidence. This is the constraint that earns
the module its place. "The header is 32 bytes" is worthless six months on; "the header
is 32 bytes — `otool -h` reports sizeofcmds 0x20" can be rechecked without redoing the
investigation. A model that cannot say where it saw something usually inferred it, and
the refusal makes it say so.

Confidence — `speculative` / `likely` / `confirmed` — is separately mandatory. RE is
mostly inference, and the failure that ruins an investigation is a plausible guess
hardening into an assumed fact because three later conclusions cited it. Recording
confidence at the point of writing is what keeps the chain auditable.

Only `question` may be recorded with nothing behind it, because most of a session is
things not yet understood and an unrecorded unknown gets rediscovered from scratch.
`hypothesis` deliberately still requires evidence: its value is being testable later,
which needs a stated reason for holding it.

An earlier version of the evidence refusal named both `hypothesis` and `question` as
the way to record something unverified, while the check exempted only `question`. A
model following the advice got the identical refusal back — a loop with no exit, in
the one place where the whole design depends on the model complying. The lesson is
that a refusal is part of the interface: if it recommends an action, that action has
to work, and a test now asserts it.

### Findings are append-only; corrections supersede

Being wrong and then right is the normal shape of the work, and the fact that a
conclusion changed is itself a finding. A correction is a new finding naming the id it
supersedes; the earlier file is never modified. Same reasoning as the append-only
transcript in ADR 0007.

The supersede relationship therefore lives only on the newer finding, which means
nothing inside the older file says it was retracted. Every read path has to add that
itself, and the first implementation only did it in the index — so reading by kind or
by id handed back a retracted conclusion with no marking at all. Worse than not
finding it: the model re-adopts something it had already corrected. Both read paths now
annotate the returned view, while leaving the file untouched.

### RE mode cannot run the artifact

The command allowlist from ADR 0011 splits into two tables. The coding table keeps the
test runners, interpreters and build tools. The inspection table has none of them —
no `python`, no `make`, no `node` — and instead holds `file`, `strings`, `nm`, `otool`,
`objdump`, `dwarfdump`, `xxd`, `c++filt` and the rest of the static-analysis set.

An unknown mode is refused rather than falling back to the default, because a typo in
a mode name must not silently grant the wider table.

The mode is injected by the loop and stripped from the model's arguments, exactly as
the sandbox and the patch journal are. This was a real hole: `run_command` grew a
`mode` parameter while the dispatcher still passed the model's arguments straight
through, so a model in RE mode could have asked for the coding table and run the thing
it was analysing. The dispatcher now overwrites it unconditionally.

Several inspection tools read by default and write when asked, which is the trap that
program-name allowlisting misses. `lipo -info` reports; `lipo -create` writes a binary.
`codesign -d` displays; `codesign -s` signs. `plutil -p` prints; `plutil -convert`
rewrites in place. Bare `unzip` extracts over the working directory. `xxd -r` reverses
a hexdump back into bytes. These carry either a forbidden-flag set or a
`required_any` set that forces the read-only form. `tar` needs its own parser, because
its mode letter arrives in a cluster that may have no leading dash: `tar tf` lists and
`tar xf` extracts, one character apart.

### Honest limits

Static inspection only. Dynamic analysis — running the target under a debugger,
watching syscalls — is a genuine need and is not solved here. The answer to it is the
VM boundary that ADR 0011 already identifies as the only real containment, not another
entry in the inspection table. The refusal message says so, so the model stops asking
and records the question instead.

Note packs are not encrypted, not access-controlled, and readable by anything that can
read the memory root. They are notes about someone else's software; treat the root
accordingly.

`notes_root()` sits outside the workspace roots, so the sandbox does not police it and
findings cannot be reached by `apply_patch`. That is deliberate — an RE run has no
`apply_patch` at all — but it does mean the notes are outside the guarantee ADR 0008
provides for everything else.

## What the live run showed

Verified against a real universal Mach-O (a renamed `/bin/echo`, so the answer was
known but not visible to the model) on the 480B `heavy` role, budget 18 steps.

It worked outside in as intended — `file`, then `otool -L`, `otool -h`, `lipo -info`,
`nm`, `strings` — and reached a correct and specific conclusion: an implementation of
`echo` from Apple's `shell_cmds-329`, citing the `@(#)PROGRAM:echo PROJECT:shell_cmds-329`
string as evidence. Five findings, each with evidence a person could recheck.

Two defects surfaced, both fixed here.

**The first attempt ran out of steps without finishing**, having spent three of
eighteen retrying shell pipes. `nm mystery_tool | head -20` was refused with "Run one
program per call", which is true and gives the model nothing to do differently, so it
tried a pipe again at step 12 and again at 13. The refusals now name the alternative
for the specific operator — for a pipe, that the full output is returned so there is
nothing to pipe into. On the re-run the model corrected on the very next step each
time, and finished in 15 steps instead of exhausting 18. Wall clock went from 184s to
65s. This is the same lesson as the evidence-refusal loop above, found twice in one
slice: **a refusal is part of the interface, and one the model cannot act on costs
real budget.**

**The exhausted run reported "Files changed: none"** while five findings sat on disk —
technically true, and exactly backwards about what the run produced. The outcome now
reports findings and pack id for an RE run, and carries both as fields so a UI does
not parse prose.

One behaviour was observed and deliberately not changed: the model recorded all its
findings in a batch at the end, five calls in five steps, despite the prompt asking it
to record as it goes. That cost 27% of the budget and means a run dying at step 9
would have left nothing. Letting `note_finding` take several findings at once would
make the batch cheap, but it treats the symptom — the durability argument wants them
written as they are established, not written more efficiently at the end. Left alone
until it causes a failure at the default 40-step budget rather than the 18 used here.

## Consequences

An RE session leaves a durable, greppable, human-readable pack behind, keyed by target
so the next session accumulates onto it rather than starting over. The loop says up
front how many findings already exist and tells the model to read them first, because
re-deriving last week's conclusions is the most wasteful thing an RE session can do.

The coding lane is untouched: same prompt, same tools, same allowlist. The two modes
share the parsing rules, the sandbox and the dispatcher, and differ only in the three
things that actually differ.
