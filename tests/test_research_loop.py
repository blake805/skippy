"""Research mode in the agent loop.

Research is the same think-tool-observe-finish loop with a different toolset, a
different record and one extra call at the end. What is worth pinning is everything
that differs: that the mode can reach the web and nothing else, that every page it
reads is logged as a source by the loop rather than by the model, that the answer is
written by a separate pass from the claims rather than from the transcript, and that a
run which never called finish still produces an answer from what it gathered.

Everything runs against the scripted model server and a mock HTTP transport, so no
model, no key and no network.
"""

import httpx
import pytest

import prompts
import skippy_agent
import skippy_brief
import skippy_research
from skippy_sandbox import Sandbox
from tests import fake_llm as fl

PAGE = (
    "<html><head><title>Series 4 specifications</title></head><body><main>"
    "<p>The Series 4 spindle runs to 24000 RPM and the rapid rate is 400 IPM.</p>"
    "</main></body></html>"
)


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Sandbox([str(root)])


@pytest.fixture
def briefs(tmp_path):
    return str(tmp_path / "briefs")


class FakeBackend(skippy_research.SearchBackend):
    name = "fake"

    def __init__(self, hits=None):
        self.hits = list(hits or [])
        self.queries = []

    async def search(self, client, query, max_results=5):
        self.queries.append(query)
        return list(self.hits)


def web(pages=None, hits=None) -> skippy_research.ResearchSession:
    pages = pages or {"https://widget.example/specs": PAGE}

    def handle(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True)
    backend = FakeBackend(hits if hits is not None else [
        skippy_research.SearchHit(
            title="Series 4 specifications",
            url="https://widget.example/specs",
            snippet="Rapid rate 400 IPM.",
        )
    ])
    return skippy_research.ResearchSession(backend=backend, client=client)


def fetch(url="https://widget.example/specs", call_id="c-fetch"):
    return fl.tool_call("web_fetch", call_id=call_id, url=url)


def note(sources="S1", call_id="c-note", **kwargs):
    args = {
        "claim": "The Series 4 rapid rate is 400 IPM.",
        "support": "The specifications page says the rapid rate is 400 IPM.",
        "sources": sources,
        "confidence": "likely",
    }
    args.update(kwargs)
    return fl.tool_call("note_claim", call_id=call_id, **args)


def finish(summary="Answered it.", call_id="c-finish"):
    return fl.tool_call("finish", call_id=call_id, summary=summary)


ANSWER = "The rapid rate is 400 IPM [S1].\n\nSources:\n[S1] Series 4 specifications"


async def research(box, script, llm, question="What is the Series 4 rapid rate?", **kwargs):
    llm.load(script)
    kwargs.setdefault("research", web())
    return await skippy_agent.run_research(question, box, **kwargs)


# -- the mode ---------------------------------------------------------------

def test_research_mode_offers_the_web_and_the_brief_and_no_way_to_edit(box, briefs):
    loop = skippy_agent.AgentLoop(
        "What is the Series 4 rapid rate?", box, mode="research", briefs_root=briefs
    )
    offered = {t["function"]["name"] for t in loop.tools()}

    assert {"web_search", "web_fetch", "note_claim", "read_brief"} <= offered
    # Nothing a page says should be able to become an edit or a process.
    assert "apply_patch" not in offered
    assert "run_command" not in offered
    # No filesystem either: there is no repository in front of this run.
    assert "read_file" not in offered and "grep" not in offered


def test_research_mode_uses_its_own_prompt(box, briefs):
    loop = skippy_agent.AgentLoop("Q?", box, mode="research", briefs_root=briefs)
    assert loop.transcript.messages[0]["content"] == prompts.RESEARCH_SYSTEM


def test_the_opening_asks_a_question_rather_than_setting_a_task(box, briefs):
    """A research run has no workspace roots to list, and listing repositories it
    cannot read would only invite it to try."""
    loop = skippy_agent.AgentLoop("What is the rapid rate?", box, mode="research",
                                  briefs_root=briefs)
    opening = loop.transcript.messages[1]["content"]
    assert "Question: What is the rapid rate?" in opening
    assert "Workspace roots" not in opening


def test_research_runs_get_a_shorter_default_budget(box, briefs):
    """A question forty steps of searching has not answered is not going to be
    answered by more searching."""
    loop = skippy_agent.AgentLoop("Q?", box, mode="research", briefs_root=briefs)
    assert loop.max_steps == skippy_agent.DEFAULT_RESEARCH_STEPS
    assert skippy_agent.AgentLoop("t", box).max_steps == skippy_agent.DEFAULT_MAX_STEPS


def test_the_other_modes_open_no_brief(box, tmp_path):
    assert skippy_agent.AgentLoop("t", box).brief is None
    assert skippy_agent.AgentLoop(
        "t", box, mode="re", notes_root=str(tmp_path / "n")
    ).brief is None


def test_a_research_run_cannot_reach_the_web_without_a_session(box, briefs):
    """No key or a broken backend leaves the session absent; the tools then refuse
    with something a person can act on rather than the run dying at construction."""
    loop = skippy_agent.AgentLoop("Q?", box, mode="research", briefs_root=briefs)
    assert loop.research is not None  # built from the default backend
    assert loop.research.backend.name == "tavily"


# -- the record -------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_page_read_is_logged_as_a_source_by_the_loop(box, briefs, routed_llm):
    """Mechanically, not by the model: a source recorded only when the model remembers
    is a source a run dying at step three does not have — and the citation check can
    only refuse a fabricated URL if the real ones were logged."""
    outcome = await research(box, [fetch(), finish(), fl.text(ANSWER)], routed_llm,
                             briefs_root=briefs)

    brief = skippy_brief.open_brief(briefs, brief_id=outcome.brief_id)
    sources = brief.sources()
    assert len(sources) == 1
    assert sources[0]["front"]["url"] == "https://widget.example/specs"
    # The page's text, not just its address.
    assert "400 IPM" in sources[0]["text"]
    assert outcome.sources == 1


@pytest.mark.asyncio
async def test_the_observation_tells_the_model_what_to_cite(box, briefs, routed_llm):
    """Load-bearing rather than informational: without the id, every claim the model
    tries to record is refused."""
    await research(box, [fetch(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs)

    logged = [o for o in routed_llm.observations() if "Logged as source" in o]
    assert logged and "S1" in logged[0]
    assert "note_claim" in logged[0]


@pytest.mark.asyncio
async def test_a_claim_recorded_mid_run_survives_the_run(box, briefs, routed_llm):
    outcome = await research(
        box, [fetch(), note(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs
    )
    assert outcome.status == "finished"
    assert outcome.findings == 1

    brief = skippy_brief.open_brief(briefs, brief_id=outcome.brief_id)
    assert "400 IPM" in brief.claims()[0]["text"]


@pytest.mark.asyncio
async def test_a_fabricated_citation_is_refused_mid_loop(box, briefs, routed_llm):
    """End to end: the model cites a page it never fetched and gets told what it did
    read, in time to correct itself on the next step."""
    outcome = await research(
        box,
        [fetch(), note(sources="S4"), note(sources="S1", call_id="c-note2"),
         finish(), fl.text(ANSWER)],
        routed_llm,
        briefs_root=briefs,
    )
    refusals = [o for o in routed_llm.observations() if "Cannot cite" in o]
    assert refusals
    assert "S4" in refusals[0]
    assert outcome.findings == 1


@pytest.mark.asyncio
async def test_reading_the_same_page_twice_does_not_become_two_sources(box, briefs, routed_llm):
    outcome = await research(
        box, [fetch(), fetch(call_id="c-fetch2"), finish(), fl.text(ANSWER)],
        routed_llm, briefs_root=briefs,
    )
    assert outcome.sources == 1


@pytest.mark.asyncio
async def test_the_loop_says_something_when_pages_pile_up_without_a_claim(box, briefs, routed_llm):
    """Same mechanism as the RE recording nudge, and the same reason: the prompt
    already asks for record-as-you-go and a model left alone batches it to the end."""
    pages = {f"https://widget.example/p{n}": PAGE for n in range(6)}
    script = [fetch(f"https://widget.example/p{n}", call_id=f"c{n}") for n in range(6)]
    script += [finish(), fl.text(ANSWER)]
    routed_llm.load(script)
    await skippy_agent.run_research(
        "What is the rapid rate?", box, briefs_root=briefs, research=web(pages),
    )

    nudges = [
        message["content"]
        for request in routed_llm.requests
        for message in request["messages"]
        if message.get("role") == "user"
        and "without recording a claim" in (message.get("content") or "")
    ]
    assert nudges


# -- synthesis --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_answer_is_written_by_a_pass_that_only_sees_the_brief(box, briefs, routed_llm):
    """The researching model ends holding twenty pages of untrusted page text and a
    pull toward whatever it read last. The synthesis call cannot be pulled anywhere,
    because the pages are not in front of it."""
    outcome = await research(
        box, [fetch(), note(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs
    )
    assert outcome.answer.startswith("The rapid rate is 400 IPM [S1].")

    synthesis = routed_llm.last_messages()
    assert synthesis[0]["content"] == prompts.RESEARCH_SYNTHESIS
    request = synthesis[1]["content"]
    assert "Series 4 rapid rate" in request
    assert "[S1]" in request
    # The page text itself is deliberately absent; only the claim and the citation go.
    assert "24000 RPM" not in request


@pytest.mark.asyncio
async def test_the_answer_is_written_into_the_brief(box, briefs, routed_llm):
    outcome = await research(
        box, [fetch(), note(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs
    )
    brief = skippy_brief.open_brief(briefs, brief_id=outcome.brief_id)
    assert "400 IPM [S1]" in brief.read_answer()


@pytest.mark.asyncio
async def test_a_run_that_ran_out_of_steps_still_answers_from_what_it_gathered(
    box, briefs, routed_llm
):
    """Refusing to write the answer because the model never called finish would throw
    away a run that read its sources and recorded its claims."""
    routed_llm.load([fetch(), note(), fl.text("Still thinking about it."), fl.text(ANSWER)])
    outcome = await skippy_agent.run_research(
        "What is the rapid rate?", box, briefs_root=briefs, research=web(), max_steps=3,
    )

    assert outcome.status != "finished"
    assert outcome.answer
    # And the synthesis pass is told the run was cut short, so the answer it writes
    # does not read as a complete one.
    assert "rather than finishing" in routed_llm.last_messages()[1]["content"]


@pytest.mark.asyncio
async def test_a_run_that_read_nothing_writes_no_answer(box, briefs, routed_llm):
    """Synthesizing from an empty brief would produce an answer out of the model's own
    knowledge with a citations heading under it — the most convincing way to be wrong."""
    routed_llm.load([finish("Could not reach anything.")])
    outcome = await skippy_agent.run_research(
        "What is the rapid rate?", box, briefs_root=briefs, research=web(),
    )
    assert outcome.answer == ""
    # The synthesis call was never made: the script still has its reply.
    assert routed_llm.remaining == 0
    assert routed_llm.call_count == 1


@pytest.mark.asyncio
async def test_a_dead_endpoint_at_synthesis_time_still_hands_back_the_research(
    box, briefs, routed_llm
):
    """The claims and sources are already on disk, so a failed synthesis costs the
    prose and nothing else."""
    routed_llm.load([fetch(), note(), finish()] + [fl.http_error(500)] * 3)
    outcome = await skippy_agent.run_research(
        "What is the rapid rate?", box, briefs_root=briefs, research=web(),
    )
    assert outcome.status == "finished"
    assert "400 IPM" in outcome.answer
    assert outcome.brief_id in outcome.answer


# -- continuing an earlier brief --------------------------------------------

@pytest.mark.asyncio
async def test_asking_the_same_question_again_opens_the_earlier_brief(box, briefs, routed_llm):
    first = await research(
        box, [fetch(), note(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs
    )
    routed_llm.load([finish(), fl.text(ANSWER)])
    again = skippy_agent.AgentLoop(
        "What is the Series 4 rapid rate?", box, mode="research", briefs_root=briefs,
    )
    assert again.brief.brief_id == first.brief_id

    opening = again.transcript.messages[1]["content"]
    assert "1 source(s), 1 claim(s)" in opening
    # Re-reading pages someone already read is the most wasteful thing a research run
    # can do, so the loop says so rather than hoping the model calls read_brief.
    assert "read_brief" in opening


@pytest.mark.asyncio
async def test_a_second_run_can_cite_the_first_runs_sources(box, briefs, routed_llm):
    """The sources are the brief's, not the run's. A follow-up should be able to build
    on them without fetching them again."""
    await research(box, [fetch(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs)

    outcome = await research(
        box, [note(), finish(), fl.text(ANSWER)], routed_llm, briefs_root=briefs
    )
    assert outcome.findings == 1


# -- what the rest of the system sees ---------------------------------------

@pytest.mark.asyncio
async def test_the_research_is_written_into_project_memory_with_sources_and_a_date(
    box, briefs, routed_llm, tmp_path
):
    memory_root = str(tmp_path / "projects")
    outcome = await research(
        box, [fetch(), note(), finish(), fl.text(ANSWER)], routed_llm,
        briefs_root=briefs, memory_root=memory_root,
    )

    import skippy_memory
    memory = skippy_memory.open_project(root=memory_root, workspace_roots=list(box.roots))
    notes = memory.research()
    assert len(notes) == 1
    text = notes[0]["text"]
    assert "400 IPM" in text
    assert "https://widget.example/specs" in text
    assert notes[0]["front"]["researched"]
    assert notes[0]["front"]["brief"] == outcome.brief_id

    # And it is findable by subject, which is what stops the same question being
    # researched from scratch a third time.
    assert "400 IPM" in memory.recall("rapid rate series").content


@pytest.mark.asyncio
async def test_the_done_event_carries_the_answer(box, briefs, routed_llm):
    events = []

    async def emit(event):
        events.append(event)

    routed_llm.load([fetch(), note(), finish(), fl.text(ANSWER)])
    await skippy_agent.run_research(
        "What is the rapid rate?", box, briefs_root=briefs, research=web(), emit=emit,
    )

    done = [e for e in events if e["type"] == "agent_done"][0]
    assert "400 IPM" in done["answer"]
    assert done["sources"] == 1
    assert any(e["type"] == "agent_synthesis" for e in events)


def test_the_wire_protocol_can_ask_for_research():
    """A research run is a run like any other from the client's side: it takes a slot,
    streams its steps and can be cancelled."""
    from skippy_tasks import agent_mode_for

    assert agent_mode_for("Research") == "research"
    assert agent_mode_for("web") == "research"
    assert agent_mode_for("Agent") == "coding"


@pytest.mark.asyncio
async def test_the_transcript_only_ever_grows(box, briefs, routed_llm):
    """The same contract as every other mode: mlx_lm.server caches by prefix, and a
    rewritten turn costs a full re-prefill."""
    await research(
        box, [fetch(), note(), fetch(call_id="c2"), finish(), fl.text(ANSWER)],
        routed_llm, briefs_root=briefs,
    )
    # The synthesis call is a fresh conversation rather than an extension, so it is
    # excluded from the prefix check by construction: it is the last request.
    history = [r["messages"] for r in routed_llm.requests[:-1]]
    for index in range(1, len(history)):
        assert history[index][:len(history[index - 1])] == history[index - 1]
