# 0013 — Project memory: continuity that admits when it is stale

Status: accepted
Date: 2026-07-30
Implements the schema accepted in [ADR 0003](0003-session-schema.md). Follows 0010
(agent loop) and 0012 (RE mode).

## Context

ADR 0003 accepted a session and decision schema in Phase 3 and it was never built in
this lineage. `sessions_root()` existed and nothing wrote to it. What memory there was
— `save_memory` / `search_memory` in `tools.py` — was a single global Chroma collection
of free-text facts with no project scoping, no provenance and no notion of going out
of date.

So every session started blind. It re-derived the same architecture, re-found where
things live, and re-explored dead ends, because a failed approach leaves no trace
anywhere: the repository records what was done, never what was tried and abandoned. The
agent got no better at working on a repo however many times it did.

## Decision

Plain files under `sessions_root()/projects/<project_id>`, keyed by workspace roots.
Sessions as JSON, decisions as markdown with YAML front matter — the same
files-are-storage choice as ADR 0012's note packs, for the same reasons: they survive
an unmounted NAS, a person can read them off the share with no Skippy running, and they
diff. Recall is deterministic keyword scoring, which also makes it testable in CI with
no embedding backend, exactly as ADR 0003 anticipated. Vector search remains a layer
that could be added over the files; it must not become the storage.

The project id is derived from the basenames of the workspace roots, sorted. Nobody has
to remember a project name for continuity to work, and listing the same roots in a
different order must not produce a second project. Several roots are one project,
because cross-repo work is the reason for having several.

### Two things ADR 0003 did not have

**Recall is not optional.** The relevant history is assembled and put in the opening
message. It is not only a tool the model may call when it feels the need.

This is the third time this project has run into the same thing. RE mode had to
announce its note pack up front rather than trusting the model to look; even with an
explicit instruction to record findings as it went, the live run batched them all at the
end. Anything that must happen has to be done by the loop, not requested of the model.
`recall_project` still exists for the case the opening block cannot cover — a decision
forty sessions back — but the common case does not depend on the model choosing to ask.

**Memory says when it has gone stale.** This is what separates memory that helps from
memory that actively hurts. "The retry logic lives in `client.py`" is confidently wrong
once that file is split, and the model believes it, because it arrives labelled as
established project knowledge rather than as a guess. Being wrong here is worse than
knowing nothing: a blind session reads the code, while a misinformed one goes looking
for a file that is not there.

So every entry records the commit it was written at and the paths it concerns, and on
the way out any path that no longer exists is marked
`[MAY BE OUT OF DATE: … no longer exists]`. Decisions superseded by later ones are
excluded from the opening context and marked in recall. The opening block also says, in
words, that it is a record rather than an instruction and that the code wins where they
disagree — without which a stale note reads as a directive.

This is the same lesson as the superseded findings in ADR 0012, and the mechanism is
deliberately the same shape.

### Written automatically, decided deliberately

The session record — task, status, summary, files changed, commit, step count — is
written by the loop on **every** terminal outcome, including `max_steps`, `cancelled`
and `failed`. A run that ran out of steps halfway through a migration is the single most
useful thing the next session can know, and a save-on-success rule would discard exactly
that.

Decisions are model-called. A decision is a judgment with reasoning behind it, and an
extractor pointed at a transcript produces bland restatements of the diff. The schema
says so explicitly, because the failure mode is a "decision" reading "changed ops.py to
add retry" — which the diff already says and which is worth nothing later. `body` is
required: a title records what was chosen, and the reasoning is what stops it being
quietly undone.

Neither the project nor the memory object is reachable from model arguments, the same as
the sandbox, the patch journal and the RE mode.

## What the live run showed

Two sessions on the 480B against a small repo with a hard-coded retry count, the second
a fresh loop sharing nothing with the first but the memory root.

Session 1 read the code, patched it, ran the tests, recorded a decision, and finished
having made the retry count a parameter defaulting to 3. Session 2 opened with that
decision and summary in its first message, and was asked to make the retry *delay*
controllable "consistent with how the retry count is handled". It produced
`def fetch(url, get, retries=3, delay=0.01)` — matching the interface pattern it could
only have known from memory — and improved the loop so it does not sleep after the final
attempt.

It never called `recall_project`. It did not need to, because the opening message already
had what it needed. Which is the argument for that design: had the history been available
only through a tool, this session would most likely never have looked, and would have
picked its own unrelated interface.

One defect surfaced. An earlier attempt ran with the model endpoint down, so both runs
failed at step zero — and project memory dutifully recorded
`[failed] Model unavailable: Role 'heavy' ... RemoteProtocolError` as though it were
something learned about the code. In a context this small, operational noise displaces
real history. Runs that ended in `failed` or `cancelled` and produced nothing are no
longer recorded.

Drawing that line took two attempts. The first rule was "do not record a run that
produced nothing", which an existing test immediately caught as too wide: a `max_steps`
run that only read files still ran the model and called tools, and "this was attempted
and did not get there" is real history about the task. The rule is now scoped to the
statuses that mean the run never happened.

A separate observation, not a defect here: the same test on the 30B went badly — five
identical malformed `read_file` calls in one turn, an invented `ggrep` tool, and a patch
that left the file syntactically broken without the model noticing. The framework
behaved correctly throughout (loop detection fired, the sandbox and allowlist held,
`python -c` was refused), but memory quality is bounded by run quality, and the 30B is
not adequate for the planner role.

## Consequences

A second session on the same repos opens knowing what the last one did, what was decided
and why, and what was left unfinished. Verified end to end: a run recording a decision
and finishing, then a fresh loop on the same roots whose opening message contains both.

Memory failing is never fatal. An unmounted memory root costs continuity, not the run,
and a failure to write the record cannot lose work that is already on disk — both are
logged and stepped over.

The opening context is capped and recency-weighted, so it cannot grow to crowd out the
task. Older sessions stay on disk and remain greppable by hand and reachable through
`recall_project`; they are simply not worth the context they would cost.

One test-hygiene consequence worth recording: because the loop now writes a record for
every run, the suite's shared `/tmp` memory root meant each agent-loop test appended to
the same project and then read the accumulated pile back as opening context — so the
prompt a test constructed depended on how many times the suite had been run before. The
memory root is now per-test.

### Not addressed

No distillation. Fifty sessions of history are recency-truncated, not folded into a
standing summary of what is true about the project. The transcript has `fold()` for
this and memory does not; when the opening context starts losing things worth keeping,
that is the shape of the answer.

Records are keyed to workspace-root basenames, so moving a repo to a differently named
directory starts a new project. Renaming is rare enough that inferring identity from a
git remote is not obviously worth the failure modes it would add.

Notes are not encrypted or access-controlled beyond filesystem permissions on the memory
root, and they contain task text and summaries. Same posture as the RE note packs.
