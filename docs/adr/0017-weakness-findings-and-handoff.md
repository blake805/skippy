# 0017 — Weakness findings, and the handoff from reading to fixing

Status: accepted
Date: 2026-07-30
Extends the taxonomy in [ADR 0012](0012-reverse-engineering-mode.md) and the memory schema
in [ADR 0013](0013-project-memory.md).

## Context

We harden software, firmware and hardware we build ourselves, during development and test.
A finding exists to drive a fix in our own code. It is the route to the patch, not a
deliverable for a client.

ADR 0012's taxonomy has no way to say "this should be fixed." A weak update check gets
recorded as `behavior` — accurate, and indistinguishable from a note about how a checksum
routine works. So the thing the mode exists to produce was the one thing the pack could not
mark.

The larger gap was on the other side. Even recorded perfectly, a weakness sat in a note pack
under the notes root while the fix happens in coding mode, in a different lane, with no
`apply_patch` and no shared state by design. The route from finding to patch was a person
remembering to go and read the pack. Sessions do not remember, which is why ADR 0013 exists.

## Decision

**A `weakness` kind, with a mandatory severity, and only there.** `low` / `medium` / `high`
/ `critical`. Four levels because three collapses into "bad" and "not bad" and five invites
argument about the middle.

This is fix urgency in our own code during development, not a CVSS score. A missing bounds
check in a parser that only ever sees our own signed payloads is not the same work as the
same bug on an unauthenticated network path, and a scoring rubric imported from
vulnerability disclosure would rank them together.

Severity is required for `weakness` and rejected for every other kind. A severity on a
`structure` finding is a category error, and a `weakness` without one is the thing that
cannot be triaged.

**Severity never travels without confidence.** ADR 0012 requires confidence on every
finding, and severity is the field that makes it load-bearing: a speculative critical and a
confirmed critical are entirely different work. Anywhere severity is displayed, confidence is
displayed beside it. Showing severity alone is how a guess acquires a deadline.

**The handoff is a work item in project memory.** Recording a weakness in RE mode raises a
work item, written by the loop rather than requested of the model — ADR 0013's rule again.
Coding sessions on the same workspace roots open with those items in their first message,
worst first, each naming its pack and finding id.

The item carries the title, severity, confidence, and a pointer to where the evidence is. It
does not carry the finding's body. The pack is the record and duplicating it into memory
would create two copies that drift, with the wrong one — the summary — arriving labelled as
established knowledge. Naming the location is what keeps a single source of truth, and the
coding session can read the pack.

Project memory rather than a new store because it already is this: keyed by workspace roots,
plain files, assembled into the opening context, honest about staleness. The RE session and
the coding session share workspace roots even when they share nothing else, so the handoff
needs no new keyspace. Schema version goes to 3.

**`resolve_work_item` exists in coding mode only.** A weakness is discharged by changing
code, which the RE lane cannot do; offering it there would let a session close an item it had
no means of fixing. Resolution is append-only — a separate record naming the item, never an
edit to it — so the pattern matches superseding a finding or a decision, and the history of
what was found stays intact regardless of what was later decided about it. Resolved items
stop arriving in the opening context and remain in `recall`, including the text of how they
were fixed.

Deliberately not built: any client-facing report generator. Findings drive our own patches.
A report is a different artifact for a different reader and would pull the schema towards
presentation.

## Consequences

The workflow works end to end: a weakness recorded in an RE session appears in the opening
message of a fresh coding loop that shares only the workspace roots, with its severity, its
confidence, and the pack and finding id where the evidence lives. Verified in tests, both
that it arrives and that an ordinary finding does not.

A pack's index lists weaknesses first, ordered by severity, with confidence beside each.
Superseded weaknesses drop out of that section and stay in the file.

An unmounted memory root costs the handoff, not the finding. The weakness is recorded in the
pack and the model is told the item was not raised, because its finish summary is then the
only route to a person. Same posture as every other memory write.

### Not addressed

No status beyond open and resolved. No owner, no due date, no "accepted risk", no "will not
fix". Those are the fields that turn this into an issue tracker, which we have, and the
severity ordering plus a resolution record covers what a session needs to know.

Nothing detects that a weakness has been fixed. A work item stays open until a coding
session resolves it, so an item fixed by hand outside Skippy keeps arriving. Inferring
resolution from a diff would be guessing about the one thing where a wrong guess is
expensive.

Work items are not deduplicated. Two RE sessions that both notice the same unsigned update
raise two items. Matching them would mean matching titles across sessions, which is the kind
of near-miss comparison that quietly drops the second one.
