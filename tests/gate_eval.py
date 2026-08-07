"""Scoring the research gate against a labelled set, so its threshold is derived.

The number in `skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD` decides how often Skippy
interrupts himself to go and check something. Picked by feel it would be unarguable and
unmovable — nobody can tell whether 0.7 is better than 0.6 by reading the code. So it is
picked here instead, from `tests/fixtures/gate_cases.json`, by the same sweep the test
runs on every commit.

**The costs are asymmetric and that is the whole design.** A missed check leaves a
confidently wrong answer standing, which is the failure this capability exists to
prevent. A needless check spends part of a budget and lands a follow-up message the user
did not need. Both are real, the first is worse, and `MISS_COST` / `FALSE_ALARM_COST`
are where that judgment is written down instead of being smuggled into a comparison
operator.

Run it:

    python -m tests.gate_eval            # the offline report, from the recorded labels
    python -m tests.gate_eval --live     # the real classifier, if a model is running

The offline mode is what CI can check, and it only measures what is deterministic: the
cheap layer's routing, and the threshold sweep over recorded self-checks. It cannot tell
you whether the classifier prompt is any good — only `--live` can, and only against a
model. Keeping both in one file is deliberate: the day the prompt changes, the same set
scores both halves.
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

import skippy_gate

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "gate_cases.json")

# A wrong answer left standing costs twice what an unnecessary check costs. Not four
# times: by the time the self-check runs the reply has already been delivered, so a
# false alarm is a follow-up message rather than an interruption — annoying, not
# damaging. Not equal either, because the entire point of the feature is the first one.
MISS_COST = 2.0
FALSE_ALARM_COST = 1.0

LABELS = ("research", "answer", "ideation")


@dataclass
class Case:
    turn: str
    label: str
    why: str = ""
    answer: str = ""
    self_check: Optional[dict] = None
    override: str = ""

    @property
    def should_research(self) -> bool:
        return self.label == "research"


def load(path: str = FIXTURE) -> List[Case]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    cases = []
    for raw in payload["cases"]:
        if raw.get("label") not in LABELS:
            raise ValueError(f"{raw.get('turn')!r} has label {raw.get('label')!r}")
        cases.append(Case(
            turn=raw["turn"],
            label=raw["label"],
            why=raw.get("why", ""),
            answer=raw.get("answer", ""),
            self_check=raw.get("self_check"),
            override=raw.get("override", ""),
        ))
    return cases


@dataclass
class Score:
    """A confusion matrix with the asymmetry already applied."""

    hits: int = 0            # should research, and would
    misses: int = 0          # should research, and would not
    false_alarms: int = 0    # should not research, but would
    correct_rejects: int = 0
    missed_turns: List[str] = field(default_factory=list)
    false_alarm_turns: List[str] = field(default_factory=list)

    def add(self, predicted: bool, actual: bool, turn: str = "") -> None:
        if actual and predicted:
            self.hits += 1
        elif actual and not predicted:
            self.misses += 1
            self.missed_turns.append(turn)
        elif not actual and predicted:
            self.false_alarms += 1
            self.false_alarm_turns.append(turn)
        else:
            self.correct_rejects += 1

    @property
    def total(self) -> int:
        return self.hits + self.misses + self.false_alarms + self.correct_rejects

    @property
    def cost(self) -> float:
        return self.misses * MISS_COST + self.false_alarms * FALSE_ALARM_COST

    @property
    def recall(self) -> float:
        wanted = self.hits + self.misses
        return self.hits / wanted if wanted else 1.0

    @property
    def precision(self) -> float:
        raised = self.hits + self.false_alarms
        return self.hits / raised if raised else 1.0

    def report(self, title: str) -> str:
        return (
            f"{title}\n"
            f"  caught {self.hits}/{self.hits + self.misses} of what should be checked "
            f"(recall {self.recall:.2f})\n"
            f"  {self.false_alarms} needless check(s) out of "
            f"{self.false_alarms + self.correct_rejects} that should be left alone "
            f"(precision {self.precision:.2f})\n"
            f"  weighted cost {self.cost:.1f}"
        )


# ---------------------------------------------------------------------------
# Layer c: what the cheap signals route to the classifier
# ---------------------------------------------------------------------------

def reaches_classifier(case: Case) -> bool:
    """Whether the cheap layer would spend a classifier call on this turn.

    Reproduces `pre_answer`'s early exits without a model. This is the number that
    matters for the trade made there: every True is a model call added to a reply's
    latency, and every False on a research case is something only the post-answer
    self-check can still catch.
    """
    found = skippy_gate.signals(case.turn)
    if found.override:
        return False
    if len(case.turn.strip()) < skippy_gate.MIN_TURN_CHARS:
        return False
    return found.score > 0


def score_cheap_layer(cases: List[Case]) -> Score:
    score = Score()
    for case in cases:
        if case.override:
            # An override is not routing, it is an instruction, and it is scored where
            # it belongs: as the final decision it actually is.
            score.add(case.override == "always", case.should_research, case.turn)
            continue
        score.add(reaches_classifier(case), case.should_research, case.turn)
    return score


# ---------------------------------------------------------------------------
# Layer b: the threshold
# ---------------------------------------------------------------------------

def would_escalate(self_check: dict, threshold: float) -> bool:
    """`post_answer`'s rule: a low number AND something concrete to check."""
    try:
        confidence = float(self_check.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    checkable = [c for c in (self_check.get("checkable") or []) if str(c).strip()]
    return confidence < threshold and bool(checkable)


def score_threshold(cases: List[Case], threshold: float) -> Score:
    score = Score()
    for case in cases:
        if case.self_check is None or case.override:
            continue
        score.add(would_escalate(case.self_check, threshold), case.should_research, case.turn)
    return score


def sweep(cases: List[Case], step: float = 0.05) -> List[tuple]:
    """(threshold, score) across the range, for picking a value and seeing the shape."""
    results = []
    steps = int(round(1.0 / step))
    for index in range(steps + 1):
        threshold = round(index * step, 2)
        results.append((threshold, score_threshold(cases, threshold)))
    return results


def best_thresholds(cases: List[Case], step: float = 0.05) -> List[float]:
    """Every threshold achieving the lowest cost, lowest first.

    A list rather than one number because the optimum is almost always a plateau — the
    cases are discrete, so nothing changes until a threshold crosses one of them. A test
    that demanded a single value would be pinning an arbitrary point inside a flat
    region and would break on the next case added.
    """
    scored = sweep(cases, step)
    lowest = min(score.cost for _, score in scored)
    return [threshold for threshold, score in scored if score.cost == lowest]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def offline_report(cases: List[Case]) -> str:
    lines = [f"{len(cases)} labelled turn(s) from {os.path.relpath(FIXTURE)}", ""]
    counts = {label: sum(1 for c in cases if c.label == label) for label in LABELS}
    lines.append("  " + ", ".join(f"{label}: {n}" for label, n in counts.items()))
    lines.append("")

    cheap = score_cheap_layer(cases)
    lines.append(cheap.report("Cheap layer — what reaches the classifier at all"))
    if cheap.missed_turns:
        lines.append("  not routed (the self-check is the only net left):")
        lines += [f"    - {turn}" for turn in cheap.missed_turns]
    lines.append("")

    lines.append("Self-check threshold sweep")
    for threshold, score in sweep(cases):
        best = " <-- shipped" if abs(threshold - skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD) < 1e-9 else ""
        lines.append(
            f"  {threshold:.2f}  cost {score.cost:5.1f}  recall {score.recall:.2f}  "
            f"precision {score.precision:.2f}{best}"
        )
    optimal = best_thresholds(cases)
    lines.append("")
    lines.append(
        f"Lowest cost at: {', '.join(f'{t:.2f}' for t in optimal)} "
        f"(shipped: {skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD:.2f})"
    )

    shipped = score_threshold(cases, skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD)
    lines.append("")
    lines.append(shipped.report("At the shipped threshold"))
    if shipped.missed_turns:
        lines.append("  still missed:")
        lines += [f"    - {turn}" for turn in shipped.missed_turns]
    if shipped.false_alarm_turns:
        lines.append("  checked needlessly:")
        lines += [f"    - {turn}" for turn in shipped.false_alarm_turns]
    return "\n".join(lines)


async def live_report(cases: List[Case], role: str = "fast") -> str:
    """Run the real pre-answer gate over the set. Needs a model server.

    The half the offline report cannot cover: whether the classifier prompt actually
    sorts these turns. Slow, costs tokens, and worth running whenever RESEARCH_GATE is
    edited — a prompt change that quietly turns every greeting into a search is exactly
    the kind of regression nothing else here would catch.
    """
    score = Score()
    lines = []
    for case in cases:
        decision = await skippy_gate.pre_answer(case.turn, role=role)
        score.add(bool(decision), case.should_research, case.turn)
        mark = "ok " if bool(decision) == case.should_research else "XX "
        lines.append(
            f"  {mark}[{case.label:8}] {case.turn[:60]:60} -> "
            f"{'research' if decision else 'answer':8} ({decision.layer})"
        )
    return "\n".join([score.report("Live gate, end to end"), ""] + lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true",
                        help="run the real classifier (needs a model server)")
    parser.add_argument("--role", default="fast", help="which model role to ask")
    args = parser.parse_args(argv)

    cases = load()
    if not args.live:
        print(offline_report(cases))
        return 0

    # The gate refuses to fire without a configured backend, which would make every
    # live case come back "answer" for the wrong reason.
    os.environ.setdefault(skippy_gate.skippy_research.TAVILY_KEY_ENV, "tvly-eval-placeholder")
    print(asyncio.run(live_report(cases, role=args.role)))
    return 0


if __name__ == "__main__":  # pragma: no cover - a tuning tool, run by hand
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raise SystemExit(main())
