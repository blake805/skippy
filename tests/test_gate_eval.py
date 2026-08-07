"""The gate, scored against the labelled set — and the threshold derived from it.

`DEFAULT_CONFIDENCE_THRESHOLD` decides how readily Skippy interrupts himself to go and
check something. Picked by feel it would be a number nobody could argue with, so it is
picked by `tests/gate_eval.py` instead, from `tests/fixtures/gate_cases.json`. These
tests are what keeps the two in step: change the constant without the data supporting
it, or add cases that move the optimum, and this goes red pointing at the report.

What is checkable offline is the deterministic half — the cheap layer's routing and the
threshold sweep over recorded self-checks. Whether the classifier prompt actually sorts
these turns needs a model, and `python -m tests.gate_eval --live` is where that lives.
"""

import pytest

import skippy_gate
import skippy_research
from tests import gate_eval


@pytest.fixture(scope="module")
def cases():
    return gate_eval.load()


@pytest.fixture(autouse=True)
def keyed(monkeypatch):
    monkeypatch.setenv(skippy_research.TAVILY_KEY_ENV, "tvly-test")


# -- the set itself ---------------------------------------------------------

def test_the_set_covers_all_three_labels_in_useful_numbers(cases):
    """A set that is all one label tunes nothing: a gate that always says no would
    score perfectly on it."""
    counts = {label: sum(1 for c in cases if c.label == label) for label in gate_eval.LABELS}
    assert all(count >= 8 for count in counts.values()), counts
    assert len(cases) >= 30


def test_every_case_says_why_it_is_labelled_that_way(cases):
    """The label is a judgment. Six months on, an unexplained one is unmaintainable —
    nobody can tell a deliberate call from a typo."""
    for case in cases:
        assert case.why.strip(), case.turn


def test_the_labelled_answers_are_realistic_about_confidence(cases):
    """A set where every correct answer reports an empty checkable list makes the
    threshold look free: precision stays at 1.0 wherever you put it, and the sweep has
    no opinion. Real self-checks list checkable claims for answers that are perfectly
    fine, which is the entire reason a threshold exists rather than just a rule.
    """
    confident_but_checkable = [
        case for case in cases
        if case.label != "research" and case.self_check
        and case.self_check.get("checkable")
    ]
    assert len(confident_but_checkable) >= 5


# -- the cheap layer's routing ----------------------------------------------

def test_ideation_never_costs_a_classifier_call(cases):
    """The most common turn in a brainstorming conversation. Every one that reaches the
    classifier is latency added to a reply, for a call whose answer we can predict."""
    for case in cases:
        if case.label == "ideation" and not case.override:
            assert not gate_eval.reaches_classifier(case), case.turn


def test_the_cheap_layer_routes_most_of_what_needs_checking(cases):
    """Not all of it, and that is the design: what it misses, the self-check behind the
    answer still catches. The bound is here so the shortcut cannot quietly rot into a
    gate that never fires."""
    score = gate_eval.score_cheap_layer(cases)
    assert score.recall >= 0.85, gate_eval.offline_report(cases)
    # And it must not send everything, or the shortcut has bought nothing.
    assert score.precision >= 0.7, gate_eval.offline_report(cases)


def test_every_override_is_obeyed(cases):
    """The user being explicit outranks every signal, in both directions."""
    for case in cases:
        if not case.override:
            continue
        found = skippy_gate.signals(case.turn)
        assert found.override == case.override, case.turn
        assert not gate_eval.reaches_classifier(case), case.turn


# -- the threshold ----------------------------------------------------------

def test_the_shipped_threshold_is_the_one_the_set_picks(cases):
    """The Phase 4 deliverable, in one assertion: the constant in skippy_gate is not a
    guess, it is the bottom of the cost curve over the labelled turns. If this fails,
    run `python -m tests.gate_eval` and either move the constant or argue with the set.
    """
    optimal = gate_eval.best_thresholds(cases)
    assert skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD in optimal, (
        f"the set now prefers {optimal}\n\n{gate_eval.offline_report(cases)}"
    )


def test_the_threshold_actually_matters(cases):
    """If every threshold scored the same, the number would be decoration and the sweep
    would be theatre. This is what caught exactly that in the first draft of the set."""
    costs = {score.cost for _, score in gate_eval.sweep(cases)}
    assert len(costs) > 3, "the sweep has no opinion; the cases are too clean"


def test_the_curve_has_a_cost_on_both_sides(cases):
    """Too low and honest hedging goes unchecked; too high and it re-verifies answers
    that were already right. A threshold with a penalty on only one side is a threshold
    that should have been a rule."""
    best = gate_eval.best_thresholds(cases)[0]
    at_best = gate_eval.score_threshold(cases, best)
    below = gate_eval.score_threshold(cases, max(0.0, best - 0.2))
    above = gate_eval.score_threshold(cases, min(1.0, best + 0.2))

    assert below.misses > at_best.misses
    assert above.false_alarms > at_best.false_alarms


def test_the_shipped_threshold_catches_most_of_what_it_is_shown(cases):
    score = gate_eval.score_threshold(cases, skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD)
    assert score.recall >= 0.9, gate_eval.offline_report(cases)
    # A needless check spends part of a conversation's budget and lands a message
    # nobody asked for, so precision here is close to free and worth demanding.
    assert score.precision >= 0.9, gate_eval.offline_report(cases)


def test_a_confident_wrong_answer_is_not_something_this_layer_can_catch(cases):
    """Stated as a test because it is the honest limit of asking a model about itself,
    and the reason there are two layers in front of this one. The Core2 case is a wrong
    number reported at 0.95: no threshold rescues it without escalating every correct
    answer alongside it — but its turn does reach the classifier, which is where a
    layered design earns its keep."""
    hard = next(c for c in cases if "16 megs" in c.turn)
    assert hard.label == "research"
    assert not gate_eval.would_escalate(
        hard.self_check, skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD
    )
    assert gate_eval.reaches_classifier(hard)


# -- the two rules that are not the threshold -------------------------------

def test_no_checkable_claims_means_no_escalation_at_any_threshold(cases):
    """The rule that stops a model's modesty about an opinion turning into a search.
    It holds regardless of where the threshold sits, which is why it is a rule."""
    for case in cases:
        if case.self_check and not case.self_check.get("checkable"):
            for threshold, _ in gate_eval.sweep(cases):
                assert not gate_eval.would_escalate(case.self_check, threshold), case.turn


def test_the_report_names_the_turns_it_got_wrong(cases):
    """A score with no examples in it is a number nobody can act on."""
    report = gate_eval.offline_report(cases)
    assert "Lowest cost at" in report
    assert "still missed" in report or "caught 16/16" in report
    assert str(skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD) in report
