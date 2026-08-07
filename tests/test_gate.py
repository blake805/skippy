"""The autonomous research trigger: when Skippy goes and checks without being asked.

Three things decide it and none of them is trusted alone, so each layer is tested for
what it is actually responsible for: the heuristics for settling the free cases and
honouring an explicit instruction, the classifier for the three-way call, the
self-check for escalating a hedged answer. Then the parts that stop it being a
nuisance — the budget, the cache, and the rule that a check never blocks a reply.

The bias under all of it is asymmetric and deliberate: a false "research" interrupts
someone's thinking, a false "answer" leaves things as they were. The tests are written
to hold that bias in place, because it is the thing that will drift.
"""

import asyncio

import pytest

import skippy_brief
import skippy_gate
import skippy_research
from skippy_gate import Conversation, Decision


@pytest.fixture(autouse=True)
def keyed(monkeypatch):
    """A configured backend, so the gate is on. No key means no autonomous checking,
    which is the right default and would otherwise make every test here a no-op."""
    monkeypatch.setenv(skippy_research.TAVILY_KEY_ENV, "tvly-test")


def replies(monkeypatch, *answers):
    """Script the fast model for the gate's classifier calls."""
    queue = list(answers)
    seen = []

    async def fake_query(messages, role="fast", **kwargs):
        seen.append(messages)
        return queue.pop(0) if queue else "{}"

    monkeypatch.setattr(skippy_gate.skippy_llm, "query_text", fake_query)
    return seen


# -- layer c: the cheap signals ---------------------------------------------

def test_recency_words_and_versions_are_what_a_model_cannot_know():
    """The strongest cheap signal there is: a model cannot know what happened after it
    was trained, and does not feel any less certain about it."""
    assert skippy_gate.signals("what is the latest version of MicroPython").recency
    assert skippy_gate.signals("is the Pi Pico 2 still available").recency
    assert skippy_gate.signals("does ESP-IDF 5.2 support that").versionish
    assert not skippy_gate.signals("how does a PID loop work").recency


def test_thinking_out_loud_counts_against_checking():
    """These are the turns where going away to read the internet is not just
    unnecessary but rude."""
    for text in (
        "what if we made the enclosure aluminum",
        "do you think that spindle is overkill",
        "which would you pick, belt or direct drive",
        "should we bother with a tool changer",
    ):
        found = skippy_gate.signals(text)
        assert found.ideation, text
        assert found.score <= 0, text


def test_a_question_of_fact_scores_above_a_question_of_taste():
    fact = skippy_gate.signals("what is the current price of a Tormach 1100MX")
    taste = skippy_gate.signals("do you think the Tormach is worth it")
    assert fact.score > taste.score


def test_hedging_is_read_from_the_answer_not_the_question():
    """A model telling on itself in prose, which it does far more reliably than it
    reports a number."""
    assert skippy_gate.signals("how fast", answer="I think it is around 400 IPM?").hedged
    assert not skippy_gate.signals("how fast", answer="It is 400 IPM.").hedged


def test_the_reason_names_what_was_noticed():
    """A decision nobody can explain is one nobody can tune."""
    reason = skippy_gate.signals("what is the latest ESP-IDF release").reason
    assert "current" in reason or "version" in reason


# -- overrides --------------------------------------------------------------

@pytest.mark.asyncio
async def test_just_tell_me_is_honoured_without_a_model_call(monkeypatch):
    seen = replies(monkeypatch, '{"decision": "research", "question": "x"}')
    decision = await skippy_gate.pre_answer(
        "just tell me roughly what the latest ESP-IDF version is"
    )
    assert not decision
    assert decision.layer == "override"
    assert seen == [], "an explicit instruction must not cost a classifier call"


@pytest.mark.asyncio
async def test_go_check_that_is_honoured_the_same_way(monkeypatch):
    seen = replies(monkeypatch, '{"decision": "ideation"}')
    decision = await skippy_gate.pre_answer("go and check whether that is still true")
    assert decision
    assert decision.layer == "override"
    assert seen == []


@pytest.mark.asyncio
async def test_an_override_also_stops_the_self_check_second_guessing_it(monkeypatch):
    """Otherwise 'just tell me' would be honoured on the way in and then quietly
    reversed on the way out."""
    replies(monkeypatch, '{"confidence": 0.1, "checkable": ["x"], "question": "x?"}')
    decision = await skippy_gate.post_answer(
        "just tell me, off the top of your head", "Probably about 400 IPM."
    )
    assert not decision
    assert decision.layer == "override"


# -- layer a: the pre-answer classifier -------------------------------------

@pytest.mark.asyncio
async def test_ideation_never_reaches_the_classifier(monkeypatch):
    """The most common turn in a brainstorming conversation, settled for free."""
    seen = replies(monkeypatch, '{"decision": "research", "question": "x"}')
    decision = await skippy_gate.pre_answer("what if we made the enclosure aluminum")
    assert not decision
    assert decision.layer == "heuristic"
    assert seen == []


@pytest.mark.asyncio
async def test_a_turn_with_nothing_to_date_it_skips_the_classifier(monkeypatch):
    """The classifier would otherwise be a model call on every reply, including on
    'good morning'. Missing here is not the last chance: the self-check runs behind the
    delivered answer and costs the user nothing."""
    seen = replies(monkeypatch, '{"decision": "research", "question": "x"}')
    for text in ("good morning skippy", "how does a feedback loop work", "thanks, that helps"):
        decision = await skippy_gate.pre_answer(text)
        assert not decision, text
        assert decision.layer == "heuristic", text
    assert seen == []


@pytest.mark.asyncio
async def test_a_factual_turn_goes_to_the_classifier_and_is_believed(monkeypatch):
    replies(
        monkeypatch,
        '{"decision": "research", "question": "What is the latest ESP-IDF release?"}',
    )
    decision = await skippy_gate.pre_answer("what is the latest ESP-IDF release")

    assert decision
    assert decision.layer == "classifier"
    assert decision.question == "What is the latest ESP-IDF release?"


@pytest.mark.asyncio
async def test_the_classifier_can_say_answer_and_be_believed(monkeypatch):
    replies(monkeypatch, '{"decision": "answer"}')
    assert not await skippy_gate.pre_answer("how does a current version of PID work")


@pytest.mark.asyncio
async def test_the_question_is_self_contained_because_the_run_cannot_see_the_chat(
    monkeypatch
):
    seen = replies(monkeypatch, '{"decision": "research", "question": "Is the Pico 2 in stock?"}')
    decision = await skippy_gate.pre_answer(
        "is it still available",
        history=[{"role": "user", "content": "I was looking at the Pico 2"}],
    )
    assert decision.question == "Is the Pico 2 in stock?"
    # The classifier gets the context it needs to resolve that pronoun.
    assert "Pico 2" in seen[0][1]["content"]


@pytest.mark.asyncio
async def test_a_broken_classifier_answers_as_before(monkeypatch):
    """A gate having a bad day should cost an unchecked answer, not a broken turn."""
    async def broken(*args, **kwargs):
        raise RuntimeError("model server down")

    monkeypatch.setattr(skippy_gate.skippy_llm, "query_text", broken)
    assert not await skippy_gate.pre_answer("what is the latest ESP-IDF release")


@pytest.mark.asyncio
async def test_garbage_from_the_classifier_downgrades_to_answering(monkeypatch):
    replies(monkeypatch, "I would probably look that up, honestly")
    assert not await skippy_gate.pre_answer("what is the latest ESP-IDF release")


@pytest.mark.asyncio
async def test_a_grunt_is_not_classified(monkeypatch):
    seen = replies(monkeypatch, '{"decision": "research", "question": "x"}')
    for text in ("ok", "thanks", "yeah", "hm"):
        assert not await skippy_gate.pre_answer(text), text
    assert seen == []


@pytest.mark.asyncio
async def test_the_gate_is_off_without_a_search_backend(monkeypatch):
    """Otherwise every autonomous check on a keyless machine ends in an apology for
    not being able to check, which is worse than never having offered."""
    monkeypatch.delenv(skippy_research.TAVILY_KEY_ENV, raising=False)
    seen = replies(monkeypatch, '{"decision": "research", "question": "x"}')
    decision = await skippy_gate.pre_answer("what is the latest ESP-IDF release")
    assert not decision
    assert decision.layer == "config"
    assert seen == []


@pytest.mark.asyncio
async def test_it_can_be_turned_off_outright(monkeypatch):
    monkeypatch.setenv(skippy_gate.AUTO_ENV, "0")
    assert not await skippy_gate.pre_answer("what is the latest ESP-IDF release")


# -- layer b: the answering model's own verdict -----------------------------

@pytest.mark.asyncio
async def test_low_confidence_with_checkable_claims_escalates(monkeypatch):
    replies(
        monkeypatch,
        '{"confidence": 0.3, "checkable": ["the rapid rate is 400 IPM"], '
        '"question": "What is the Series 4 rapid rate?"}',
    )
    decision = await skippy_gate.post_answer("how fast is it", "About 400 IPM, I think.")

    assert decision
    assert decision.layer == "self-check"
    assert decision.question == "What is the Series 4 rapid rate?"
    assert "400 IPM" in decision.reason


@pytest.mark.asyncio
async def test_low_confidence_about_an_opinion_does_not_escalate(monkeypatch):
    """A model being modest about a recommendation is not a fact to go and check, and
    researching it finds nothing. Both halves are required."""
    replies(monkeypatch, '{"confidence": 0.2, "checkable": [], "question": ""}')
    assert not await skippy_gate.post_answer(
        "which would you pick", "Belt drive, but it is a close call."
    )


@pytest.mark.asyncio
async def test_high_confidence_with_checkable_claims_does_not_escalate(monkeypatch):
    replies(
        monkeypatch,
        '{"confidence": 0.95, "checkable": ["water boils at 100C"], "question": "x?"}',
    )
    assert not await skippy_gate.post_answer("boiling point", "100 degrees at sea level.")


@pytest.mark.asyncio
async def test_the_threshold_is_configurable_for_tuning(monkeypatch):
    """Phase 4 tunes this against a labelled set rather than by feel, so it has to be
    a knob rather than a constant in a branch."""
    reply = '{"confidence": 0.6, "checkable": ["a claim"], "question": "q?"}'
    replies(monkeypatch, reply, reply)

    monkeypatch.setenv(skippy_gate.CONFIDENCE_ENV, "0.5")
    assert not await skippy_gate.post_answer("q", "an answer")
    monkeypatch.setenv(skippy_gate.CONFIDENCE_ENV, "0.8")
    assert await skippy_gate.post_answer("q", "an answer")


@pytest.mark.asyncio
async def test_a_nonsense_threshold_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(skippy_gate.CONFIDENCE_ENV, "very high")
    assert skippy_gate.confidence_threshold() == skippy_gate.DEFAULT_CONFIDENCE_THRESHOLD


@pytest.mark.asyncio
async def test_a_broken_self_check_leaves_the_answer_alone(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(skippy_gate.skippy_llm, "query_text", broken)
    assert not await skippy_gate.post_answer("q", "an answer")


# -- budget and cache -------------------------------------------------------

def test_the_budget_is_per_conversation_not_per_turn():
    conversation = Conversation(max_runs=3)
    assert conversation.allows()
    conversation.runs = 3
    assert not conversation.allows()


def test_two_wordings_of_one_question_are_one_cache_entry():
    """Keyed the way briefs are keyed, so the cache and the briefs on disk agree about
    which questions are the same question."""
    conversation = Conversation()
    conversation.remember(
        skippy_gate.Result(question="What is the max feed rate?", answer="400 IPM.")
    )
    assert conversation.recall("  what IS the max feed rate  ").answer == "400 IPM."
    assert conversation.recall("what is the spindle power") is None


@pytest.mark.asyncio
async def test_a_question_already_answered_costs_no_run(tmp_path):
    conversation = Conversation()
    conversation.remember(
        skippy_gate.Result(question="What is the max feed rate?", answer="400 IPM.")
    )
    result = await skippy_gate.check(
        "what is the max feed rate?", conversation, briefs_root=str(tmp_path / "briefs")
    )
    assert result.answer == "400 IPM."
    assert conversation.runs == 0


@pytest.mark.asyncio
async def test_an_answer_already_on_disk_costs_no_run(tmp_path):
    """The third time a subject comes up it should be free, and a brief written last
    week is exactly as good as one written now — until it is not, which is what the
    staleness mark is for."""
    briefs = str(tmp_path / "briefs")
    brief = skippy_brief.open_brief(briefs, question="What is the max feed rate?")
    brief.log_source(url="https://widget.example/specs", text="400 IPM.", title="Specs")
    brief.write_answer("The maximum feed rate is 400 IPM [S1].")

    conversation = Conversation()
    result = await skippy_gate.check(
        "What is the max feed rate?", conversation, briefs_root=briefs
    )
    assert result.cached
    assert "400 IPM" in result.answer
    assert conversation.runs == 0


@pytest.mark.asyncio
async def test_a_stale_brief_is_not_served_from_the_cache(tmp_path, monkeypatch):
    """An old answer about the web is the kind of wrong that gets believed."""
    briefs = str(tmp_path / "briefs")
    brief = skippy_brief.open_brief(briefs, question="What is the current firmware?")
    brief.log_source(url="https://widget.example/specs", text="2.7.1", title="Specs")
    brief.write_answer("Firmware 2.7.1 [S1].")
    monkeypatch.setattr(skippy_brief, "STALE_AFTER_DAYS", 0)

    assert skippy_gate.cached_answer("What is the current firmware?", briefs) is None


@pytest.mark.asyncio
async def test_a_spent_budget_refuses_rather_than_researching(tmp_path):
    conversation = Conversation(max_runs=1)
    conversation.runs = 1
    result = await skippy_gate.check(
        "something new", conversation, briefs_root=str(tmp_path / "briefs")
    )
    assert not result.answer
    assert "budget" in result.error


@pytest.mark.asyncio
async def test_a_check_spends_a_run_and_reports_the_answer(tmp_path, monkeypatch):
    """The one place the whole path is exercised: gate to loop to answer."""
    async def fake_run_research(question, sandbox, **kwargs):
        import skippy_agent
        return skippy_agent.AgentOutcome(
            status="finished", summary="Looked it up.",
            answer="The rapid rate is 400 IPM [S1].", brief_id="b1", sources=2,
        )

    import skippy_agent
    monkeypatch.setattr(skippy_agent, "run_research", fake_run_research)

    conversation = Conversation()
    result = await skippy_gate.check(
        "What is the rapid rate?", conversation, briefs_root=str(tmp_path / "briefs")
    )
    assert result.answer.startswith("The rapid rate is 400 IPM")
    assert result.sources == 2
    assert conversation.runs == 1
    # And it is remembered, so asking again is free.
    assert conversation.recall("what is the rapid rate") is not None


@pytest.mark.asyncio
async def test_a_failed_check_comes_back_as_something_sayable(tmp_path, monkeypatch):
    """Nobody is awaiting this, so a failure has to arrive as words rather than as an
    exception in a log."""
    async def explode(*args, **kwargs):
        raise RuntimeError("the internet fell over")

    import skippy_agent
    monkeypatch.setattr(skippy_agent, "run_research", explode)

    result = await skippy_gate.check(
        "a question", Conversation(), briefs_root=str(tmp_path / "briefs")
    )
    assert not result.answer
    assert "internet fell over" in result.error
    assert "could not" in skippy_gate.report(result)


@pytest.mark.asyncio
async def test_a_check_works_with_no_workspace_roots_configured(tmp_path, monkeypatch):
    """A conversation on a machine with no repositories set up must still be able to
    check something; the run has no filesystem tools either way."""
    captured = {}

    async def fake_run_research(question, sandbox, **kwargs):
        import skippy_agent
        captured["roots"] = list(sandbox.roots)
        return skippy_agent.AgentOutcome(status="finished", answer="An answer.", brief_id="b")

    import skippy_agent
    monkeypatch.setattr(skippy_agent, "run_research", fake_run_research)

    result = await skippy_gate.check(
        "a question", Conversation(), roots=[], briefs_root=str(tmp_path / "briefs")
    )
    assert result.answer == "An answer."
    assert captured["roots"]


@pytest.mark.asyncio
async def test_the_source_cap_is_passed_to_the_run(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_research(question, sandbox, **kwargs):
        import skippy_agent
        captured["session"] = kwargs.get("research")
        return skippy_agent.AgentOutcome(status="finished", answer="An answer.")

    import skippy_agent
    monkeypatch.setattr(skippy_agent, "run_research", fake_run_research)

    await skippy_gate.check(
        "a question", Conversation(max_sources=2), briefs_root=str(tmp_path / "briefs")
    )
    assert captured["session"].max_sources == 2


# -- what the lanes are handed ----------------------------------------------

def test_the_acknowledgment_is_a_note_not_a_canned_line():
    """It has to sound like whichever Skippy is talking, and a fixed string said every
    time is a tic."""
    note = skippy_gate.acknowledgment(Decision(True, question="Is it still supported?"))
    assert "SYSTEM NOTE" in note
    assert "Is it still supported?" in note
    assert "do not invent" in note
    assert "out loud" in skippy_gate.acknowledgment(
        Decision(True, question="q"), spoken=True
    )


def test_a_written_follow_up_carries_its_sources_and_a_spoken_one_does_not():
    """Out loud, a wall of citations is unusable; in a chat window it is the point."""
    result = skippy_gate.Result(
        question="q", answer="It is 400 IPM [S1].", brief_id="b1", sources=3
    )
    written = skippy_gate.report(result)
    assert "3 source(s)" in written and "b1" in written
    assert skippy_gate.report(result, spoken=True) == "It is 400 IPM [S1]."


# -- the per-run caps -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_run_stops_reading_at_its_source_cap():
    """Five sources read properly beat twelve skimmed, and the run that reads twelve
    arrives after the conversation has moved on."""
    import httpx

    def handle(request):
        return httpx.Response(200, text="<html><body><main><p>Text here.</p></main></body></html>",
                              headers={"content-type": "text/html"})

    session = skippy_research.ResearchSession(
        backend=skippy_research.TavilyBackend(api_key="x"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        max_sources=2,
    )
    assert (await skippy_research.web_fetch(session, url="https://a.example/1")).ok
    assert (await skippy_research.web_fetch(session, url="https://b.example/2")).ok
    third = await skippy_research.web_fetch(session, url="https://c.example/3")

    assert not third.ok
    assert "2 sources" in third.summary
    # A second chunk of a page already read is not a new source, so it still works.
    assert (await skippy_research.web_fetch(session, url="https://a.example/1")).ok


@pytest.mark.asyncio
async def test_a_run_stops_searching_at_its_search_cap():
    class Backend(skippy_research.SearchBackend):
        name = "fake"

        async def search(self, client, query, max_results=5):
            return []

    session = skippy_research.ResearchSession(backend=Backend(), max_searches=1)
    assert (await skippy_research.web_search(session, query="one")).ok
    second = await skippy_research.web_search(session, query="two")
    assert not second.ok
    assert "1 searches" in second.summary


def test_an_explicitly_requested_run_is_uncapped():
    """The caps exist to keep an unasked-for check small. A run somebody asked for is
    a different thing, and rationing it would be second-guessing them."""
    session = skippy_research.ResearchSession(backend=skippy_research.TavilyBackend(api_key="x"))
    assert session.max_sources is None
    assert session.max_searches is None


def test_nothing_here_needs_an_event_loop_to_import():
    """skippy_gate is imported by both lanes at module scope; a cycle or a loop
    requirement would surface as an import error at startup rather than here."""
    assert asyncio.iscoroutinefunction(skippy_gate.check)
