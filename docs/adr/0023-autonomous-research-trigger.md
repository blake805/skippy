# 0023 — Researching without being asked: a layered gate

Status: accepted
Date: 2026-08-07
Extends [0022](0022-web-research.md), which built the capability this decides when to
use. Follows the routing pattern established for the voice action lane (a cold
fast-model classifier beside a tool-free streaming persona) and the budget-and-record
reasoning of [0013](0013-project-memory.md).

## Context

ADR 0022 gave Skippy the web, reachable in a research run somebody starts. That is worth
much less than it sounds, because of when people ask: a tool you have to invoke gets
invoked exactly when the user already suspects the answer is wrong — which is the one
case where they did not need the help. The failure this is meant to prevent is the
confident, fluent, out-of-date answer that nobody thinks to question, and no
user-initiated feature can catch it by construction.

So the assistant has to notice on his own. The obvious implementation — ask the model
whether it is sure — does not work, and it fails worst in exactly the case that matters.
Self-reported confidence is poorly calibrated, and a model that has assembled a plausible
answer from stale weights does not feel less certain than one working from something it
knows. Building the trigger on that number alone would produce a feature that fires on
hedged answers about opinions and stays silent on assured answers about last year's
software.

The other half of the problem is that this can be actively unpleasant. Most of what gets
said to Skippy is thinking out loud — designs, what-ifs, "which would you pick". Going
away to read the internet in the middle of that interrupts the person for a question with
no factual answer. The costs are asymmetric and the design has to be too.

## Decision

Three layers, none trusted alone, biased toward answering, with the user able to override
in either direction — and nothing that makes the conversation wait.

### 1. Cheap signals, first and free

Regexes over the turn: recency words, version numbers and years, capitalized product
names on one side; the vocabulary of ideation on the other. These settle the easy cases
with no model call, which is the same trade `wants_action` already makes before the voice
router, and they gate whether layer 2 runs at all.

### 2. A fast-model classifier before the answer

Three-way — needs-research / answer-directly / pure-ideation — as a cold call with no
stake in the answer, which is precisely what makes it better evidence than asking the
model that is about to answer. It runs only when layer 1 found something to date the
question by. That shortcut is a real trade: it means a bare factual question whose
wording advertises nothing ("which python version does mlx need") is not caught here.
The alternative was a classifier call added to the latency of every reply including
"good morning", which in the voice lane is the second the whole lane exists to protect.

### 3. The answering model's verdict, afterwards

Behind the delivered reply, so it costs no latency anyone waits through — which is what
makes it affordable to ask the main model rather than a cheap one. It reports a
confidence and, more usefully, the specific statements it just made that could be wrong.
Escalation requires **both** a confidence below threshold and a non-empty list: a low
number with nothing concrete behind it is a model being modest about a recommendation,
and researching that finds nothing.

### The threshold is derived from a labelled set, not chosen

`tests/fixtures/gate_cases.json` holds labelled turns with the self-check each answer
produced, and `python -m tests.gate_eval` sweeps the threshold against them under a cost
model that prices a missed check at twice a needless one — asymmetric because a missed
check leaves a wrong answer standing while a needless one costs a follow-up message,
and by that point the reply has already been delivered. A test fails if the shipped
constant drifts from what the set picks.

This is worth the trouble for a reason beyond the number itself: a threshold in a source
file is unarguable and therefore unmovable, and the first version of the set demonstrated
why. Every correct answer in it reported an empty checkable list, so precision was 1.0 at
every threshold and the sweep had no opinion at all — the data, not the code, was wrong.
That is not a mistake anyone finds by reading a constant.

### Overrides outrank everything

"Just tell me" and "go check that" are honoured without a model call, in both directions,
and an override on the way in also suppresses the self-check on the way out — otherwise
an instruction would be obeyed and then quietly reversed. The labelled set caught the
first draft of this too: it matched "look it up" and not "look up what the torque is".

### Nothing blocks, and there are budgets

The turn is answered immediately with a system note telling the persona it is verifying
— a note rather than a canned line, so the acknowledgment sounds like whichever Skippy is
talking. The check runs beside the conversation, never in the client's one run slot, and
the result arrives on its own: in chat as a `chat` event carrying `kind: "research"`
after `done`, and out loud as a spoken announcement trimmed to three sentences with the
citations left in the brief.

Three runs per conversation, five sources and six searches per run, with caching in the
session, in the brief on disk and in project memory. An explicitly requested run is
uncapped: the caps exist to keep an unasked-for check small, and rationing something the
user asked for is second-guessing them. With no search backend configured the whole gate
is off, because otherwise every autonomous check on a keyless machine ends in an apology
for not being able to check.

## Consequences

Skippy corrects himself unprompted, which is the point, and the correction arrives as a
distinct message with sources rather than as a silently better answer — the user can see
that a check happened and what it was based on.

Layer 2's shortcut shifts weight onto layer 3, the layer whose calibration is least
trustworthy. The eval reports exactly which labelled turns fall through, and the fix if
that proves wrong in use is one line: drop the cheap-signal precondition and pay the
classifier call on every turn.

There is one failure no threshold can reach, and the set contains an example on purpose:
an answer that is wrong and reported at 0.95. Layer 3 cannot catch it without escalating
every correct answer alongside it. That case is caught, if at all, by layer 2 — which is
the entire argument for the trigger being layered rather than being a number.

Cost and noise are now conversational concerns rather than API concerns. Three runs per
conversation is a guess informed by nothing yet; it is deliberately a small number, on
the grounds that a Skippy who checks three things well is better company than one who
checks eight and follows up all afternoon.
