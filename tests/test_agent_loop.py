"""The agent loop.

Three groups of invariants. The transcript contract, because native tool calling
requires every tool call to be answered and mlx_lm.server's prompt cache requires
the transcript to only ever grow. Honest stop reasons, because a run that ran out
of steps must never be reported as a success. And not getting stuck, because the
failure mode of an unattended loop is burning forty steps repeating itself.
"""

import pytest

import skippy_agent
from skippy_sandbox import Sandbox
from tests import fake_llm as fl


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# calc\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


async def run(box, script, llm, **kwargs):
    llm.load(script)
    return await skippy_agent.run_task("Do the thing", box, **kwargs)


def finish(summary="done", files=None, call_id="call_1"):
    args = {"summary": summary}
    if files is not None:
        args["files_changed"] = files
    return fl.tool_call("finish", call_id=call_id, **args)


# --- the transcript contract ---

@pytest.mark.asyncio
async def test_every_tool_call_gets_exactly_one_answer(box, routed_llm):
    """Native tool calling requires it. An assistant turn with three calls and two
    tool messages is malformed, and it surfaces later as confused output rather
    than as an error, which makes it worth pinning down here."""
    await run(box, [
        fl.tool_calls(
            ("read_file", {"path": "calc/ops.py"}),
            ("list_dir", {"path": "."}),
            ("glob_files", {"pattern": "**/*.py"}),
        ),
        finish(),
    ], routed_llm)

    messages = routed_llm.last_messages()
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        expected = [c["id"] for c in message["tool_calls"]]
        answers = []
        for following in messages[index + 1:]:
            if following.get("role") != "tool":
                break
            answers.append(following["tool_call_id"])
        assert answers == expected, f"assistant turn {index} was answered with {answers}"


@pytest.mark.asyncio
async def test_calls_after_finish_are_still_answered(box, routed_llm):
    """The loop stops running tools at finish but still owes an answer for the rest,
    or it leaves a malformed turn behind for anything that resumes the session.

    Checked against the loop's own transcript, because the final turn is never sent
    to the model — so asserting on the outcome alone would prove nothing.
    """
    routed_llm.load([
        fl.tool_calls(
            ("finish", {"summary": "done"}),
            ("read_file", {"path": "calc/ops.py"}),
            ("list_dir", {"path": "."}),
        ),
    ])
    loop = skippy_agent.AgentLoop("t", box)
    outcome = await loop.run()
    assert outcome.status == "finished"

    messages = loop.transcript.messages
    assistant = next(m for m in messages if m.get("tool_calls"))
    answered = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert answered == [c["id"] for c in assistant["tool_calls"]]
    # The skipped ones are answered, and say why they were not run.
    assert "Not executed" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_the_transcript_only_ever_grows(box, routed_llm):
    """mlx_lm.server caches by prefix; rewriting an earlier message costs a full
    re-prefill, measured at ~60s against ~3s on the heavy role."""
    await run(box, [
        fl.tool_call("read_file", path="calc/ops.py"),
        fl.tool_call("grep", pattern="def add"),
        fl.tool_call("list_dir", path="."),
        finish(),
    ], routed_llm)
    assert routed_llm.prefix_broken_at() is None


@pytest.mark.asyncio
async def test_observations_reach_the_model_as_tool_messages(box, routed_llm):
    await run(box, [fl.tool_call("read_file", path="calc/ops.py"), finish()], routed_llm)
    observations = routed_llm.observations()
    assert any("def add(a, b):" in o for o in observations)
    assert any(o.startswith("OK: ") for o in observations)


@pytest.mark.asyncio
async def test_the_task_and_the_roots_are_in_the_opening_message(box, routed_llm):
    routed_llm.load([finish()])
    await skippy_agent.run_task("Rename add to plus", box)
    opening = routed_llm.last_messages()[1]["content"]
    assert "Rename add to plus" in opening
    assert str(box.roots[0]) in opening


@pytest.mark.asyncio
async def test_the_system_prompt_leads_the_transcript(box, routed_llm):
    routed_llm.load([finish()])
    await skippy_agent.run_task("t", box)
    first = routed_llm.last_messages()[0]
    assert first["role"] == "system"
    assert "Skippy" in first["content"]


# --- what the model is offered ---

@pytest.mark.asyncio
async def test_the_loop_offers_the_workspace_tools_and_finish(box, routed_llm):
    routed_llm.load([finish()])
    await skippy_agent.run_task("t", box)
    offered = set(routed_llm.tools_offered())
    assert {"read_file", "grep", "list_dir", "glob_files", "apply_patch", "finish"} <= offered


@pytest.mark.asyncio
async def test_the_loop_drives_the_heavy_role_by_default(box, routed_llm):
    routed_llm.load([finish()])
    await skippy_agent.run_task("t", box)
    # Kept on one role so the prompt cache stays warm across steps (ADR 0001).
    assert skippy_agent.AgentLoop("t", box).role == "heavy"


# --- honest stop reasons ---

@pytest.mark.asyncio
async def test_finish_is_the_only_success(box, routed_llm):
    outcome = await run(box, [finish("Renamed add to plus", files=["calc/ops.py"])], routed_llm)
    assert outcome.status == "finished"
    assert outcome.ok
    assert outcome.summary == "Renamed add to plus"
    assert outcome.files_changed == ["calc/ops.py"]


@pytest.mark.asyncio
async def test_running_out_of_steps_is_not_success_even_after_real_edits(box, routed_llm, repo):
    """The tempting bug: files changed, so call it a win. But the model never
    decided it was done, and hiding that turns a stalled run into a silent one."""
    edit = fl.tool_call(
        "apply_patch",
        edits=[{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}],
    )
    outcome = await run(box, [edit] * 6, routed_llm, max_steps=3)
    assert outcome.status == "max_steps"
    assert not outcome.ok
    assert outcome.steps == 3
    # The work is real and reported, it is just not called success.
    assert "a + b + 0" in (repo / "calc" / "ops.py").read_text()
    assert "calc/ops.py" in outcome.summary


@pytest.mark.asyncio
async def test_a_model_that_stops_calling_tools_is_nudged_once_then_accepted(box, routed_llm):
    outcome = await run(box, [
        fl.text("I think the file looks fine."),
        fl.text("Yes, it is fine."),
    ], routed_llm)
    assert outcome.status == "stopped_without_finish"
    assert not outcome.ok
    assert routed_llm.call_count == 2


@pytest.mark.asyncio
async def test_the_nudge_tells_the_model_what_to_do(box, routed_llm):
    await run(box, [fl.text("Hmm."), finish()], routed_llm)
    nudges = [
        m["content"] for m in routed_llm.last_messages()
        if m.get("role") == "user" and "did not call a tool" in (m.get("content") or "")
    ]
    assert nudges and "finish" in nudges[0]


@pytest.mark.asyncio
async def test_a_narrating_turn_followed_by_work_recovers(box, routed_llm):
    """One prose turn is usually the model narrating, not stopping."""
    outcome = await run(box, [
        fl.text("Let me look at the file first."),
        fl.tool_call("read_file", path="calc/ops.py"),
        finish("done"),
    ], routed_llm)
    assert outcome.status == "finished"


@pytest.mark.asyncio
async def test_an_unreachable_model_fails_the_run_rather_than_looking_finished(box, routed_llm):
    outcome = await run(box, [fl.http_error(500)] * 5, routed_llm)
    assert outcome.status == "failed"
    assert not outcome.ok
    assert "Model unavailable" in outcome.summary


@pytest.mark.asyncio
async def test_a_finish_with_no_summary_still_reports_something(box, routed_llm):
    outcome = await run(box, [fl.tool_call("finish", summary="")], routed_llm)
    assert outcome.status == "finished"
    assert outcome.summary.strip()


# --- not getting stuck ---

@pytest.mark.asyncio
async def test_a_repeated_identical_call_is_interrupted(box, routed_llm):
    call = fl.tool_call("read_file", path="calc/ops.py")
    await run(box, [call] * 4 + [finish()], routed_llm)
    observations = routed_llm.observations()
    assert any("identical arguments" in o for o in observations)


@pytest.mark.asyncio
async def test_alternating_between_two_calls_is_also_caught(box, routed_llm):
    """The realistic stuck pattern. A compare-with-previous check never sees it,
    which is why the loop keeps a window."""
    a = fl.tool_call("read_file", path="calc/ops.py")
    b = fl.tool_call("read_file", path="README.md", call_id="call_2")
    await run(box, [a, b, a, b, a, b, finish()], routed_llm)
    assert any("identical arguments" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_varying_calls_are_never_interrupted(box, routed_llm, repo):
    for index in range(6):
        (repo / f"file{index}.py").write_text(f"x = {index}\n")
    script = [fl.tool_call("read_file", path=f"file{index}.py") for index in range(6)]
    await run(box, script + [finish()], routed_llm)
    assert not any("identical arguments" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_the_step_budget_is_clamped(box):
    assert skippy_agent.AgentLoop("t", box, max_steps=99999).max_steps == skippy_agent.HARD_MAX_STEPS
    assert skippy_agent.AgentLoop("t", box, max_steps=0).max_steps == 1


@pytest.mark.asyncio
async def test_cancellation_stops_the_run_at_the_next_step(box, routed_llm):
    routed_llm.load([fl.tool_call("read_file", path="calc/ops.py")] * 5)
    loop = skippy_agent.AgentLoop("t", box)

    async def cancel_after_first(event):
        if event.get("type") == "agent_tool_result":
            loop.cancel()

    loop._emit = cancel_after_first
    outcome = await loop.run()
    assert outcome.status == "cancelled"
    assert outcome.steps == 1


# --- tool failures are recoverable, not fatal ---

@pytest.mark.asyncio
async def test_a_hallucinated_tool_name_gets_the_real_list_back(box, routed_llm):
    await run(box, [fl.tool_call("edit_file", path="x"), finish()], routed_llm)
    observation = "\n".join(routed_llm.observations())
    assert "Unknown tool 'edit_file'" in observation
    # Naming the real tools is what turns this into a recoverable mistake.
    assert "apply_patch" in observation and "read_file" in observation


@pytest.mark.asyncio
async def test_bad_arguments_are_explained(box, routed_llm):
    await run(box, [fl.tool_call("read_file", wrong_arg="x"), finish()], routed_llm)
    observation = "\n".join(routed_llm.observations())
    assert "Bad arguments" in observation
    assert "path" in observation


@pytest.mark.asyncio
async def test_a_sandbox_escape_is_an_observation_not_a_crash(box, routed_llm):
    outcome = await run(box, [
        fl.tool_call("read_file", path="../../../etc/passwd"),
        finish("could not read it"),
    ], routed_llm)
    assert outcome.status == "finished"
    assert any("Sandbox violation" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_malformed_arguments_do_not_strand_the_run(box, routed_llm):
    routed_llm.load([fl.malformed_tool_call("read_file"), finish()])
    outcome = await skippy_agent.run_task("t", box)
    assert outcome.status == "finished"
    assert any("not valid JSON" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_a_rejected_patch_lets_the_model_retry(box, routed_llm, repo):
    outcome = await run(box, [
        fl.tool_call("apply_patch", edits=[
            {"path": "calc/ops.py", "search": "def subtract", "replace": "def minus"},
        ]),
        fl.tool_call("apply_patch", edits=[
            {"path": "calc/ops.py", "search": "def add", "replace": "def plus"},
        ]),
        finish("renamed"),
    ], routed_llm)
    assert outcome.status == "finished"
    assert "def plus" in (repo / "calc" / "ops.py").read_text()
    assert any("byte-for-byte" in o for o in routed_llm.observations())


# --- real work through the loop ---

@pytest.mark.asyncio
async def test_a_multi_file_change_lands_and_is_reported(box, routed_llm, repo):
    outcome = await run(box, [
        fl.tool_call("apply_patch", edits=[
            {"path": "calc/ops.py", "search": "def add", "replace": "def plus"},
            {"path": "README.md", "search": "# calc", "replace": "# calc (renamed)"},
        ]),
        finish("renamed add to plus"),
    ], routed_llm)

    assert outcome.status == "finished"
    assert "def plus" in (repo / "calc" / "ops.py").read_text()
    assert set(outcome.files_changed) == {"calc/ops.py", "README.md"}


@pytest.mark.asyncio
async def test_a_dry_run_does_not_count_as_a_file_change(box, routed_llm, repo):
    outcome = await run(box, [
        fl.tool_call(
            "apply_patch",
            dry_run=True,
            edits=[{"path": "calc/ops.py", "search": "def add", "replace": "def plus"}],
        ),
        finish("previewed only"),
    ], routed_llm)
    assert outcome.files_changed == []
    assert "def add" in (repo / "calc" / "ops.py").read_text()


@pytest.mark.asyncio
async def test_the_journal_directory_is_injected_not_chosen_by_the_model(
    box, routed_llm, tmp_path, repo
):
    """An agent that can redirect its own pre-images can arrange for them not to exist."""
    journal = tmp_path / "journal"
    routed_llm.load([
        fl.tool_call("apply_patch", journal_dir="/tmp/somewhere-else", edits=[
            {"path": "calc/ops.py", "search": "def add", "replace": "def plus"},
        ]),
        finish("done"),
    ])
    await skippy_agent.run_task("t", box, journal_dir=str(journal))
    assert journal.exists()
    assert list(journal.iterdir())


@pytest.mark.asyncio
async def test_a_model_supplied_journal_dir_is_ignored_when_none_is_configured(
    box, tmp_path, repo
):
    """The dangerous case, and the one the injection test above cannot see: with no
    journal configured there is nothing to overwrite the model's value with, so the
    pop is the only thing standing between it and writing pre-images to a path of
    its choosing — outside the sandbox, since the journal is not sandbox-checked."""
    import skippy_dispatch

    planted = tmp_path / "planted"
    result = await skippy_dispatch.dispatch(
        "apply_patch",
        {
            "journal_dir": str(planted),
            "edits": [{"path": "calc/ops.py", "search": "def add", "replace": "def plus"}],
        },
        box,
        journal_dir=None,
    )
    assert result.ok
    assert not planted.exists()
    assert result.data["journal"] is None


@pytest.mark.asyncio
async def test_a_model_supplied_sandbox_is_ignored(box):
    import skippy_dispatch

    result = await skippy_dispatch.dispatch(
        "read_file", {"sandbox": "/etc", "path": "calc/ops.py"}, box
    )
    assert result.ok
    assert "def add" in result.content


@pytest.mark.asyncio
async def test_tool_counts_and_steps_are_reported(box, routed_llm):
    outcome = await run(box, [
        fl.tool_call("read_file", path="calc/ops.py"),
        fl.tool_calls(("list_dir", {"path": "."}), ("glob_files", {"pattern": "*.md"})),
        finish(),
    ], routed_llm)
    assert outcome.steps == 3
    # Three real tools; finish is control flow, not a tool call.
    assert outcome.tool_calls == 3


# --- the event stream ---

@pytest.mark.asyncio
async def test_the_event_stream_narrates_the_run(box, routed_llm):
    events = []
    routed_llm.load([
        fl.tool_call("read_file", thought="Let me look.", path="calc/ops.py"),
        finish("done"),
    ])
    await skippy_agent.run_task("t", box, emit=lambda e: _collect(events, e))

    kinds = [e["type"] for e in events]
    assert kinds[0] == "agent_start"
    assert kinds[-1] == "agent_done"
    assert "agent_thought" in kinds
    assert "agent_tool_call" in kinds
    assert "agent_tool_result" in kinds


async def _collect(events, event):
    events.append(event)


@pytest.mark.asyncio
async def test_a_patch_event_carries_the_diff(box, routed_llm):
    events = []
    routed_llm.load([
        fl.tool_call("apply_patch", edits=[
            {"path": "calc/ops.py", "search": "def add", "replace": "def plus"},
        ]),
        finish(),
    ])
    await skippy_agent.run_task("t", box, emit=lambda e: _collect(events, e))
    patches = [e for e in events if e["type"] == "agent_patch"]
    assert len(patches) == 1
    assert "-def add(a, b):" in patches[0]["diff"]
    assert patches[0]["files"][0]["path"] == "calc/ops.py"


@pytest.mark.asyncio
async def test_a_broken_event_sink_does_not_kill_the_run(box, routed_llm, repo):
    """The run is the valuable thing; a disconnected UI can reconnect. Losing an
    in-progress multi-file edit because a socket closed would be the wrong trade."""
    async def explode(event):
        raise RuntimeError("socket closed")

    routed_llm.load([
        fl.tool_call("apply_patch", edits=[
            {"path": "calc/ops.py", "search": "def add", "replace": "def plus"},
        ]),
        finish("done"),
    ])
    outcome = await skippy_agent.run_task("t", box, emit=explode)
    assert outcome.status == "finished"
    assert "def plus" in (repo / "calc" / "ops.py").read_text()


def test_redaction_keeps_events_small_but_informative():
    redacted = skippy_agent.redact({
        "edits": [{"path": "a.py", "action": "edit", "search": "x" * 5000, "replace": "y" * 5000}],
        "content": "z" * 2000,
        "path": "b.py",
    })
    # Edits keep their shape, because that is what the UI shows, but not their bodies.
    assert redacted["edits"] == [{"path": "a.py", "action": "edit"}]
    assert len(redacted["content"]) < 700
    assert redacted["path"] == "b.py"


# --- oversized observations ---

@pytest.mark.asyncio
async def test_an_oversized_observation_is_compressed_before_the_model_sees_it(
    box, routed_llm, repo
):
    """The heavy role prefills at ~200 tok/s, so raw tool output is paid for on every
    later step, not just the one that produced it."""
    (repo / "huge.py").write_text("# padding\n" * 3000)
    routed_llm.load([
        fl.tool_call("read_file", path="huge.py"),
        fl.text("compressed digest of the file"),  # the compressor's reply
        finish("done"),
    ])
    await skippy_agent.run_task("t", box)
    observations = routed_llm.observations()
    assert any("[compressed from" in o for o in observations)
    assert all(len(o) < skippy_agent.COMPRESS_THRESHOLD + 500 for o in observations)


@pytest.mark.asyncio
async def test_a_small_observation_is_passed_through_untouched(box, routed_llm):
    await run(box, [fl.tool_call("read_file", path="calc/ops.py"), finish()], routed_llm)
    assert not any("[compressed" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_failed_compression_falls_back_to_truncation(box, routed_llm, repo, monkeypatch):
    (repo / "huge.py").write_text("# padding\n" * 3000)

    async def broken_compress(*args, **kwargs):
        raise RuntimeError("compressor down")

    monkeypatch.setattr(skippy_agent.skippy_llm, "compress", broken_compress)
    outcome = await run(box, [
        fl.tool_call("read_file", path="huge.py"),
        finish("done"),
    ], routed_llm)
    assert outcome.status == "finished"
    assert any("compression unavailable" in o for o in routed_llm.observations())


# --- construction ---

def test_a_run_needs_a_task(box):
    with pytest.raises(ValueError):
        skippy_agent.AgentLoop("   ", box)


def test_extra_context_reaches_the_opening_message(box):
    loop = skippy_agent.AgentLoop("t", box, extra_context="Prior session: renamed foo.")
    assert "Prior session: renamed foo." in loop.transcript.messages[1]["content"]


def test_dispatch_never_exposes_the_sandbox_as_a_model_argument():
    """The model choosing its own roots would defeat the point of having them."""
    import skippy_dispatch
    assert "sandbox" not in skippy_dispatch._expected("read_file")
    assert "journal_dir" not in skippy_dispatch._expected("apply_patch")
