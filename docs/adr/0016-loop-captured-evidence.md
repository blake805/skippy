# 0016 — The loop records the evidence; the model records the conclusions

Status: accepted
Date: 2026-07-30
Fourth application of the rule stated in [ADR 0013](0013-project-memory.md). Amends
[ADR 0012](0012-reverse-engineering-mode.md).

## Context

ADR 0012's prompt asks the model to record findings as it goes, and gives the reason: a run
that dies leaves only what it has already written. The first live RE run batched all five
findings into the last five steps of eighteen. A failure at step nine would have produced
nothing at all — not a partial pack, not a list of commands tried, nothing.

The instruction was not ignored. The model recorded findings, and every one was accurate.
It just did the recording when it felt finished, which is what a model does with an
instruction about *when* rather than *what*.

ADR 0013 had already drawn this conclusion twice — the note pack has to announce itself
rather than trusting the model to look; project memory has to assemble the opening context
rather than offering a `recall_project` tool and hoping — and named it: anything that must
happen has to be done by the loop, not requested of the model. It also named RE mode's
record-as-you-go as the one place where the rule was still only a prompt. This ADR closes
that.

## Decision

Split the requirement in two, because the two halves have different owners.

**Evidence is mechanical, so the loop writes it.** Every `run_command` that actually ran in
RE mode is appended to the pack: the command, the working directory, the exit code, and its
output. The model is not asked, cannot decline, and cannot forget. A run that dies at step
nine now leaves nine commands and everything they printed.

This is worth having beyond durability. A finding's `evidence` field is the model's account
of what a tool printed; the command log is what it printed. Checking one against the other
was previously only possible by re-running the command against an artifact that may since
have changed — see [ADR 0015](0015-note-pack-identity.md) for how often that happens.

Output is capped at 20,000 characters per entry with the middle elided and the elision
marked. An `objdump` of a large binary is not evidence at any length, and an unbounded log
turns the notes root into a place where flash images go to be duplicated.

Only commands that ran are logged. A refused command produced no output about the target,
and a log where most entries are rejections buries the evidence it exists to keep.

**Conclusions are judgment, so the model still writes them.** There is no mechanical
substitute. An extractor over a transcript produces restatements of tool output, which is
the same failure ADR 0013 identified for auto-extracted decisions: "ran `otool -h` and saw
a header" is worth nothing to the next reader. Confidence in particular cannot be inferred
from the outside — whether an observation is speculative or confirmed is exactly the thing
being recorded.

So the loop does what it can here, which is to notice and say so. After six inspection
commands with no finding recorded, an observation is appended stating the count, that the
evidence is already saved and the conclusions are not, and that `question` is the kind to
use for what is not yet understood. The counter resets when it fires and when a finding is
recorded, so it recurs at an interval rather than every step: a nudge on every step is one
the model reads past, which would leave us worse off than the prompt.

Six is short enough that a drifting run is caught within a few steps and long enough that a
normal opening sequence — `file`, `strings`, `nm`, a header dump — is not interrupted before
there is anything honest to say.

The count is also reported in the run's summary and outcome, so a `max_steps` run says
"3 finding(s), 14 command(s) logged" rather than reading as having produced nothing.

## Consequences

A run that dies mid-way leaves the evidence. Verified by scripting a loop that runs
inspection commands and hits `max_steps` without recording anything, then asserting the
commands and their output are on disk and the outcome says so.

The nudge fires only when it should. Verified in both directions: a run that inspects
without recording gets it, and a run recording as it goes never does.

Coding mode is unaffected — no pack, and the diff plus the patch journal are already the
durable record there.

Failing to write a log entry is logged and stepped over. The command has already run and
the model already has its output; losing the entry costs durability, not the investigation.
Same posture as ADR 0013's memory writes.

### Not addressed

The nudge is still an instruction the model may decline. The loop could refuse to dispatch
further commands until something is recorded, and that trades a lost conclusion for a
stalled investigation — worse, and unpleasant to debug. The evidence being safe regardless
is what makes the softer mechanism acceptable here; it was not acceptable when the prompt
was the only thing standing between a crash and an empty pack.

The log is not deduplicated. The same command run twice appears twice, which is accurate
and occasionally the interesting part.

Nothing links a finding to the log entry it came from. The finding names its command in
`evidence` and a reader greps for it. An explicit reference would want the model to pass an
entry id, which is a thing to get wrong for a lookup that costs nothing by hand.
