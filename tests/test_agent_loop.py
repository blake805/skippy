"""The agent loop.

Three groups of invariants. The transcript contract, because native tool calling
requires every tool call to be answered and mlx_lm.server's prompt cache requires
the transcript to only ever grow. Honest stop reasons, because a run that ran out
of steps must never be reported as a success. And not getting stuck, because the
failure mode of an unattended loop is burning forty steps repeating itself.
"""

import os
from unittest import mock

import pytest

import skippy_agent
import skippy_dispatch
from skippy_sandbox import Sandbox, ToolResult
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


# --- reverse-engineering mode ---
#
# The two modes differ in what the loop hands the model: RE mode can read, inspect
# and record, but has no way to change the artifact or to run it. Everything below
# is about that boundary holding at the loop level, since that is where the mode is
# chosen.

def tool_names(loop):
    return {t["function"]["name"] for t in loop.tools()}


def test_re_mode_offers_notes_tools_and_no_way_to_edit(box, tmp_path):
    loop = skippy_agent.AgentLoop(
        "Work out the file format", box, mode="re", notes_root=str(tmp_path / "notes")
    )
    offered = tool_names(loop)
    assert {"note_finding", "read_notes"} <= offered
    # The artifact is not ours to change, and an RE run that edited it would have
    # destroyed the evidence for its own findings.
    assert "apply_patch" not in offered


def test_coding_mode_offers_editing_and_no_notes_tools(box):
    offered = tool_names(skippy_agent.AgentLoop("Fix the bug", box))
    assert "apply_patch" in offered
    assert "note_finding" not in offered


def test_the_two_modes_use_different_system_prompts(box, tmp_path):
    import prompts

    coding = skippy_agent.AgentLoop("t", box)
    re_loop = skippy_agent.AgentLoop("t", box, mode="re", notes_root=str(tmp_path / "n"))
    assert coding.transcript.messages[0]["content"] == prompts.AGENT_SYSTEM
    assert re_loop.transcript.messages[0]["content"] == prompts.RE_SYSTEM


def test_an_unknown_mode_is_refused_at_construction(box):
    """Rather than falling back to coding, which would silently grant the wider
    command table and the ability to edit."""
    with pytest.raises(ValueError, match="Unknown mode"):
        skippy_agent.AgentLoop("t", box, mode="reverse-engineering")


def test_re_mode_opens_a_pack_keyed_by_the_target(box, tmp_path):
    notes = str(tmp_path / "notes")
    first = skippy_agent.AgentLoop("Look at it", box, mode="re", notes_root=notes,
                                   target="/opt/libfoo.dylib")
    again = skippy_agent.AgentLoop("Look again", box, mode="re", notes_root=notes,
                                   target="/opt/libfoo.dylib")
    assert first.notes_pack.pack_id == again.notes_pack.pack_id


def test_coding_mode_opens_no_pack(box):
    assert skippy_agent.AgentLoop("t", box).notes_pack is None


def test_an_earlier_session_is_pointed_out_in_the_opening_message(box, tmp_path):
    """Re-deriving last week's conclusions is the most wasteful thing an RE session
    can do, so the loop says so up front rather than hoping the model asks."""
    import skippy_re

    notes = str(tmp_path / "notes")
    first = skippy_agent.AgentLoop("Look at it", box, mode="re", notes_root=notes,
                                   target="/opt/libfoo.dylib")
    skippy_re.note_finding(
        first.notes_pack, kind="structure", title="Header is 32 bytes",
        body="Load commands start at 0x20.", evidence="otool -h", confidence="confirmed",
    )

    resumed = skippy_agent.AgentLoop("Continue", box, mode="re", notes_root=notes,
                                     target="/opt/libfoo.dylib")
    opening = resumed.transcript.messages[1]["content"]
    assert "1 finding" in opening
    assert "read_notes" in opening


def test_a_fresh_pack_does_not_claim_prior_findings(box, tmp_path):
    loop = skippy_agent.AgentLoop("New target", box, mode="re",
                                  notes_root=str(tmp_path / "notes"), target="/opt/new.bin")
    assert "0 finding" in loop.transcript.messages[1]["content"]


@pytest.mark.asyncio
async def test_a_finding_recorded_mid_run_survives_the_run(box, tmp_path, routed_llm):
    """The notes are the deliverable, so they have to be written as findings are
    established rather than assembled at the end — a run that stops early should
    still leave behind what it learned before it stopped."""
    import skippy_re

    notes = str(tmp_path / "notes")
    routed_llm.load([
        fl.tool_call(
            "note_finding", call_id="c1",
            kind="structure", title="Magic is 0xCAFEBABE",
            body="The first four bytes are 0xCAFEBABE, a fat Mach-O.",
            evidence="xxd -l 4 shows cafebabe", confidence="confirmed",
        ),
        finish("Identified the container format.", call_id="c2"),
    ])
    outcome = await skippy_agent.run_task(
        "Identify the format", box, mode="re", notes_root=notes, target="/opt/libfoo.dylib"
    )
    assert outcome.status == "finished"

    pack = skippy_re.open_pack(notes, target="/opt/libfoo.dylib")
    assert "0xCAFEBABE" in skippy_re.read_notes(pack).content


@pytest.mark.asyncio
async def test_the_notes_tools_are_unavailable_in_coding_mode(box, routed_llm):
    """A coding run that called note_finding would get a pack-less failure; the
    message has to send it somewhere useful instead of just refusing."""
    outcome = await run(box, [
        fl.tool_call("note_finding", call_id="c1", kind="structure", title="x",
                     body="y", evidence="z", confidence="likely"),
        finish(call_id="c2"),
    ], routed_llm)

    observations = routed_llm.observations()
    assert any("finish summary" in o for o in observations)
    assert outcome.status == "finished"


@pytest.mark.asyncio
async def test_re_mode_cannot_run_the_artifact_through_the_loop(box, routed_llm, tmp_path):
    """The end-to-end version of the mode split: the loop picks the command table, so
    a model in RE mode asking to execute something is refused with the reason."""
    routed_llm.load([
        fl.tool_call("run_command", call_id="c1", command="python -m pytest"),
        finish("Cannot run it.", call_id="c2"),
    ])
    await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes"), target="x.bin"
    )
    assert any("static inspection" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_re_mode_can_still_inspect(box, routed_llm, tmp_path, repo):
    (repo / "sample.bin").write_bytes(b"\x00MAGICSTRING\x00" + b"\xff" * 32)
    routed_llm.load([
        fl.tool_call("run_command", call_id="c1", command="strings -n 6 sample.bin"),
        finish("Found a string.", call_id="c2"),
    ])
    await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes"), target="sample.bin"
    )
    assert any("MAGICSTRING" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_an_re_run_out_of_steps_reports_findings_not_files(box, tmp_path, routed_llm):
    """An RE run never changes a file, so "files changed: none" is true and actively
    misleading about four findings sitting on disk. Observed on the first live run.
    """
    notes = str(tmp_path / "notes")
    routed_llm.load([
        fl.tool_call("note_finding", call_id="c1", kind="structure",
                     title="Universal binary with two slices",
                     body="x86_64 and arm64e.", evidence="lipo -info reports both",
                     confidence="confirmed"),
        # No finish: the run hits the step ceiling with work already recorded.
        fl.tool_call("read_notes", call_id="c2"),
    ])
    outcome = await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=notes, target="x.bin", max_steps=2
    )

    assert outcome.status == "max_steps"
    assert "Findings recorded: 1" in outcome.summary
    assert "Files changed" not in outcome.summary
    # Available as data too, so a UI does not have to read the prose.
    assert outcome.findings == 1
    assert outcome.pack_id


# --- the loop records the evidence; the model records the conclusions ---
#
# ADR 0013's rule, third application: anything that must happen has to be done by the
# loop. The prompt asked the first live RE run to record findings as it went and it
# batched all five into the last five steps of eighteen, so a run dying at step nine
# would have left nothing at all.

def re_command(command, call_id="c1"):
    return fl.tool_call("run_command", call_id=call_id, command=command)


@pytest.mark.asyncio
async def test_every_inspection_command_is_logged_without_being_asked(
    box, routed_llm, tmp_path, repo
):
    import skippy_re

    (repo / "sample.bin").write_bytes(b"\x00MAGICSTRING\x00" + b"\xff" * 32)
    notes = str(tmp_path / "notes")
    routed_llm.load([
        re_command("strings -n 6 sample.bin"),
        re_command("file sample.bin", call_id="c2"),
        finish("Looked at it.", call_id="c3"),
    ])
    outcome = await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=notes, target="sample.bin"
    )

    assert outcome.commands_logged == 2
    pack = skippy_re.open_pack(notes, target="sample.bin")
    logged = "\n".join(open(p, encoding="utf-8").read() for p in pack.command_files())
    # The command and its output, so a person can recheck a finding against what the
    # tool actually printed rather than against the model's account of it.
    assert "strings -n 6 sample.bin" in logged
    assert "MAGICSTRING" in logged


@pytest.mark.asyncio
async def test_a_run_that_records_nothing_still_leaves_the_evidence(
    box, routed_llm, tmp_path, repo
):
    """The durability argument, stated exactly. This run establishes things and dies
    before writing a single finding, which is the case that previously lost everything.
    """
    import skippy_re

    (repo / "sample.bin").write_bytes(b"\x00MAGICSTRING\x00")
    notes = str(tmp_path / "notes")
    routed_llm.load([re_command("strings -n 6 sample.bin")] * 4)
    outcome = await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=notes, target="sample.bin", max_steps=2
    )

    assert outcome.status == "max_steps"
    assert outcome.findings == 0
    assert outcome.commands_logged >= 1
    # Reported in the prose too, so the run does not read as having produced nothing.
    assert "command(s) logged" in outcome.summary
    pack = skippy_re.open_pack(notes, target="sample.bin")
    assert "MAGICSTRING" in open(pack.command_files()[0], encoding="utf-8").read()


@pytest.mark.asyncio
async def test_a_refused_command_is_not_logged(box, routed_llm, tmp_path):
    """A rejected command produced no output about the target, and logging refusals
    would bury the evidence in noise."""
    import skippy_re

    notes = str(tmp_path / "notes")
    routed_llm.load([
        re_command("python -m pytest"),
        finish("Cannot run it.", call_id="c2"),
    ])
    await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=notes, target="x.bin"
    )
    assert skippy_re.open_pack(notes, target="x.bin").command_files() == []


@pytest.mark.asyncio
async def test_a_coding_run_logs_no_commands(box, routed_llm):
    """There is no pack in coding mode, and the diff is already the durable record."""
    outcome = await run(box, [
        fl.tool_call("run_command", command="python -m pytest -q"),
        finish(call_id="c2"),
    ], routed_llm)
    assert outcome.commands_logged == 0


@pytest.mark.asyncio
async def test_the_loop_says_something_when_findings_lag_behind_commands(
    box, routed_llm, tmp_path, repo
):
    """The command log keeps the evidence but cannot capture a conclusion, so the loop
    counts and quotes the number back. The prompt already asks for record-as-you-go;
    what is new here is how far past that the run has drifted."""
    (repo / "sample.bin").write_bytes(b"\x00DATA\x00")
    script = [
        re_command(f"strings -n {n} sample.bin", call_id=f"c{n}")
        for n in range(skippy_agent.RE_RECORD_NUDGE_AFTER + 1)
    ]
    routed_llm.load(script + [finish("done", call_id="cf")])
    await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes"),
        target="sample.bin",
    )

    nudges = [
        m["content"] for m in routed_llm.last_messages()
        if m.get("role") == "user" and "without recording a finding" in (m.get("content") or "")
    ]
    assert nudges, "the loop never mentioned that nothing had been recorded"
    # Has to name a way out, or it is a complaint rather than an instruction.
    assert "question" in nudges[0]


@pytest.mark.asyncio
async def test_recording_a_finding_resets_the_count(box, routed_llm, tmp_path, repo):
    """A run that is recording as it goes must never be nudged; a nudge that fires
    anyway is noise the model learns to read past."""
    (repo / "sample.bin").write_bytes(b"\x00DATA\x00")
    script = []
    for n in range(skippy_agent.RE_RECORD_NUDGE_AFTER + 2):
        script.append(re_command(f"strings -n {n} sample.bin", call_id=f"c{n}"))
        script.append(fl.tool_call(
            "note_finding", call_id=f"f{n}", kind="structure",
            title=f"Observation {n}", body="Something specific.",
            evidence=f"strings -n {n} printed it", confidence="likely",
        ))
    routed_llm.load(script + [finish("done", call_id="cf")])
    await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes"),
        target="sample.bin",
    )

    assert not [
        m for m in routed_llm.last_messages()
        if m.get("role") == "user" and "without recording a finding" in (m.get("content") or "")
    ]


# --- the disassembly tools are RE-only, and their output is evidence too ---

def test_the_disassembly_tools_are_offered_only_in_re_mode(box, tmp_path):
    """They read the session's target artifact, and a coding session has a repository
    rather than a target. Offering them there would be a tool that cannot work."""
    coding = skippy_agent.AgentLoop("Fix it", box)
    re_loop = skippy_agent.AgentLoop(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes")
    )
    for tool in ("disassemble_function", "decompile", "list_symbols"):
        assert tool in tool_names(re_loop)
        assert tool not in tool_names(coding)


def test_the_extraction_tools_are_offered_only_in_re_mode(box, tmp_path):
    """Extraction writes into the pack's quarantine, which only an RE session has."""
    coding = skippy_agent.AgentLoop("Fix it", box)
    re_loop = skippy_agent.AgentLoop(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes")
    )
    for tool in ("extract_artifact", "list_extracted"):
        assert tool in tool_names(re_loop)
        assert tool not in tool_names(coding)


@pytest.mark.asyncio
async def test_an_extraction_is_logged_to_the_pack_as_evidence(box, routed_llm, tmp_path, repo):
    """The tree that came out of an image is part of the record of what the image was, and
    the container invocation beside it is what lets someone reproduce the carve."""
    import skippy_re

    (repo / "firmware.bin").write_bytes(b"\x00" * 64)
    notes = str(tmp_path / "notes")
    routed_llm.load([
        fl.tool_call("extract_artifact", call_id="c1"),
        finish("Carved it.", call_id="c2"),
    ])

    async def fake_extract(pack, path="", depth=None):
        return ToolResult(
            True, "Extracted firmware.bin to quarantine/0001-firmware-bin: 3 file(s).",
            "Formats identified: squashfs_v4_le, gzip",
            data={"quarantine": "0001-firmware-bin", "file_count": 3,
                  "command": "podman run --rm --network none ... unblob@sha256:abc"},
        )

    with mock.patch.dict(skippy_dispatch._ASYNC_TOOLS, {"extract_artifact": fake_extract}):
        outcome = await skippy_agent.run_task(
            "Analyse it", box, mode="re", notes_root=notes, target="firmware.bin"
        )

    assert outcome.commands_logged == 1
    pack = skippy_re.open_pack(notes, target="firmware.bin")
    logged = "\n".join(open(p, encoding="utf-8").read() for p in pack.command_files())
    assert "squashfs_v4_le" in logged
    assert "--network none" in logged


@pytest.mark.asyncio
async def test_a_disassembly_is_logged_to_the_pack_like_any_other_evidence(
    box, routed_llm, tmp_path, repo
):
    """ADR 0016 says the loop keeps the evidence. A function's disassembly is evidence
    exactly as much as an `objdump` region is, and that it arrives through a structured
    tool rather than `run_command` would be a poor reason for the record to have a hole
    in it."""
    import skippy_re

    (repo / "sample.bin").write_bytes(b"\x00\x01\x02\x03")
    notes = str(tmp_path / "notes")
    routed_llm.load([
        fl.tool_call("disassemble_function", call_id="c1", symbol="verify_image"),
        finish("Looked at one function.", call_id="c2"),
    ])

    async def fake_disassemble(pack, symbol, arch="", bits=None):
        return ToolResult(
            True, f"Disassembled {symbol} at 0x1000.",
            "0x1000  push rbp\n0x1001  mov rbp, rsp",
            data={"command": "rizin -N -q -c 's 0x1000; af; pdf' sample.bin",
                  "symbol": symbol},
        )

    with mock.patch.dict(skippy_dispatch._ASYNC_TOOLS,
                         {"disassemble_function": fake_disassemble}):
        outcome = await skippy_agent.run_task(
            "Analyse it", box, mode="re", notes_root=notes, target="sample.bin"
        )

    assert outcome.commands_logged == 1
    pack = skippy_re.open_pack(notes, target="sample.bin")
    logged = "\n".join(open(p, encoding="utf-8").read() for p in pack.command_files())
    assert "push rbp" in logged
    # The invocation is recorded too, so a reader can reproduce it by hand.
    assert "rizin -N" in logged
    # Named for what was asked, not for the rizin command line: an absolute path plus a
    # `-c` script makes both the filename and the pack index unreadable.
    assert any(
        "disassemble-function-verify-image" in os.path.basename(p)
        for p in pack.command_files()
    )


@pytest.mark.asyncio
async def test_listing_symbols_is_not_logged_as_evidence(box, routed_llm, tmp_path, repo):
    """Navigation, not evidence. Logging it would pad the record without adding to it,
    and a session that only listed symbols has established nothing to record."""
    import skippy_re

    (repo / "sample.bin").write_bytes(b"\x00\x01")
    notes = str(tmp_path / "notes")
    routed_llm.load([
        fl.tool_call("list_symbols", call_id="c1"),
        finish("done", call_id="c2"),
    ])

    async def fake_list(pack, contains=""):
        return ToolResult(True, "2 symbol(s).", "0x1000  main\n0x1100  verify")

    with mock.patch.dict(skippy_dispatch._ASYNC_TOOLS, {"list_symbols": fake_list}):
        outcome = await skippy_agent.run_task(
            "Analyse it", box, mode="re", notes_root=notes, target="sample.bin"
        )

    assert outcome.commands_logged == 0
    assert skippy_re.open_pack(notes, target="sample.bin").command_files() == []


@pytest.mark.asyncio
async def test_a_failed_disassembly_is_not_logged(box, routed_llm, tmp_path, repo):
    """Same rule as a refused command: it says nothing about the artifact."""
    import skippy_re

    (repo / "sample.bin").write_bytes(b"\x00\x01")
    notes = str(tmp_path / "notes")
    routed_llm.load([
        fl.tool_call("disassemble_function", call_id="c1", symbol="nope"),
        finish("done", call_id="c2"),
    ])

    async def fake_fail(pack, symbol, arch="", bits=None):
        return ToolResult(False, "'nope' is not in this binary's 12 symbols.")

    with mock.patch.dict(skippy_dispatch._ASYNC_TOOLS,
                         {"disassemble_function": fake_fail}):
        outcome = await skippy_agent.run_task(
            "Analyse it", box, mode="re", notes_root=notes, target="sample.bin"
        )

    assert outcome.commands_logged == 0
    assert skippy_re.open_pack(notes, target="sample.bin").command_files() == []


@pytest.mark.asyncio
async def test_reading_functions_without_recording_still_gets_nudged(
    box, routed_llm, tmp_path, repo
):
    """The nudge counts inspection, and reading functions is inspection. A session that
    decompiled six routines and recorded nothing is the exact drift ADR 0016 is about."""
    (repo / "sample.bin").write_bytes(b"\x00\x01")
    script = [
        fl.tool_call("decompile", call_id=f"c{n}", symbol=f"fn_{n}")
        for n in range(skippy_agent.RE_RECORD_NUDGE_AFTER + 1)
    ]
    routed_llm.load(script + [finish("done", call_id="cf")])

    async def fake_decompile(pack, symbol, arch="", bits=None):
        return ToolResult(True, f"Decompiled {symbol}.", "int f(void) { return 0; }",
                          data={"command": f"rizin -N ... {symbol}", "symbol": symbol})

    with mock.patch.dict(skippy_dispatch._ASYNC_TOOLS, {"decompile": fake_decompile}):
        await skippy_agent.run_task(
            "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes"),
            target="sample.bin",
        )

    assert [
        m for m in routed_llm.last_messages()
        if m.get("role") == "user" and "without recording a finding" in (m.get("content") or "")
    ]


# --- a weakness found in RE mode becomes work in coding mode ---

def weakness_call(call_id="c1", severity="critical", **kwargs):
    args = {
        "kind": "weakness",
        "title": "Firmware update accepts unsigned images",
        "body": "The updater checks a CRC and no signature.",
        "evidence": "strings shows no verify call; no signature section in the header",
        "confidence": "confirmed",
        "severity": severity,
    }
    args.update(kwargs)
    return fl.tool_call("note_finding", call_id=call_id, **args)


@pytest.mark.asyncio
async def test_a_weakness_becomes_a_work_item_a_coding_session_opens_with(
    box, routed_llm, tmp_path
):
    """The workflow this exists for: find it in RE mode, fix it in coding mode. Both
    modes open the same project memory from the same workspace roots, which is what
    lets the handoff work with no new keyspace."""
    notes = str(tmp_path / "notes")
    routed_llm.load([weakness_call(), finish("Recorded a weakness.", call_id="c2")])
    outcome = await skippy_agent.run_task(
        "Audit the updater", box, mode="re", notes_root=notes,
        target="/opt/products/gate/firmware.bin",
    )
    assert outcome.work_items

    # A fresh coding loop sharing nothing with the RE run but the workspace roots.
    coding = skippy_agent.AgentLoop("Harden the updater", box)
    opening = coding.transcript.messages[1]["content"]
    assert "unsigned images" in opening
    assert "critical" in opening
    # Names where the evidence is, rather than standing in for it.
    assert outcome.pack_id in opening


@pytest.mark.asyncio
async def test_the_model_is_told_the_handoff_happened(box, routed_llm, tmp_path):
    routed_llm.load([weakness_call(), finish("done", call_id="c2")])
    await skippy_agent.run_task(
        "Audit it", box, mode="re", notes_root=str(tmp_path / "notes"), target="x.bin"
    )
    assert any("work item" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_an_ordinary_finding_raises_no_work_item(box, routed_llm, tmp_path):
    """Only a weakness has somewhere else to go. Turning every finding into a work
    item would make the coding session's opening block useless."""
    routed_llm.load([
        fl.tool_call("note_finding", call_id="c1", kind="structure",
                     title="Header is 32 bytes", body="Load commands at 0x20.",
                     evidence="otool -h reports sizeofcmds 0x20", confidence="confirmed"),
        finish("done", call_id="c2"),
    ])
    outcome = await skippy_agent.run_task(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes"), target="x.bin"
    )
    assert outcome.work_items == []
    assert skippy_agent.AgentLoop("Fix something", box).transcript.messages[1]["content"]


@pytest.mark.asyncio
async def test_a_weakness_without_project_memory_still_records_and_says_so(
    box, routed_llm, tmp_path
):
    """An unmounted memory root costs the handoff, not the finding. But the model has
    to be told, because its finish summary is then the only route to a person."""
    import skippy_re

    notes = str(tmp_path / "notes")
    routed_llm.load([weakness_call(), finish("done", call_id="c2")])
    outcome = await skippy_agent.run_task(
        "Audit it", box, mode="re", notes_root=notes, target="x.bin", remember=False
    )

    assert outcome.work_items == []
    assert outcome.findings == 1
    assert "unsigned images" in skippy_re.read_notes(
        skippy_re.open_pack(notes, target="x.bin")
    ).content
    assert any("not raised as a work item" in o for o in routed_llm.observations())


def test_resolving_a_work_item_is_offered_only_in_coding_mode(box, tmp_path):
    """A weakness is discharged by changing code, which RE mode cannot do. Offering it
    there would let a session close an item it had no way to have fixed."""
    coding = skippy_agent.AgentLoop("Fix it", box)
    re_loop = skippy_agent.AgentLoop(
        "Analyse it", box, mode="re", notes_root=str(tmp_path / "notes")
    )
    assert "resolve_work_item" in tool_names(coding)
    assert "resolve_work_item" not in tool_names(re_loop)


# --- the target can change underneath a pack ---

def test_a_changed_target_warns_in_the_opening_message(box, tmp_path):
    """The model has no way to notice on its own, and findings about bytes that have
    since changed are worse than no findings."""
    import skippy_re

    target = tmp_path / "firmware.bin"
    target.write_bytes(b"version one")
    notes = str(tmp_path / "notes")

    first = skippy_agent.AgentLoop("Look at it", box, mode="re", notes_root=notes,
                                   target=str(target))
    skippy_re.note_finding(
        first.notes_pack, kind="structure", title="Payload starts at 0x40",
        body="The header is 64 bytes.", evidence="xxd -l 64", confidence="confirmed",
    )

    target.write_bytes(b"version two, rebuilt with different bytes")
    resumed = skippy_agent.AgentLoop("Look again", box, mode="re", notes_root=notes,
                                     target=str(target))
    assert "WARNING" in resumed.transcript.messages[1]["content"]
    assert "have changed" in resumed.transcript.messages[1]["content"]


@pytest.mark.asyncio
async def test_a_coding_run_out_of_steps_still_reports_files(box, routed_llm):
    routed_llm.load([
        fl.tool_call("read_file", call_id="c1", path="calc/ops.py"),
        fl.tool_call("read_file", call_id="c2", path="README.md"),
    ])
    outcome = await skippy_agent.run_task("Fix it", box, max_steps=2)
    assert outcome.status == "max_steps"
    assert "Files changed" in outcome.summary
    assert outcome.findings == 0


# --- project memory across sessions ---
#
# The criterion this exists for: a second session on the same repos continues the
# first instead of starting blind. Everything here is about the handoff.

@pytest.mark.asyncio
async def test_a_run_is_recorded_in_project_memory(box, tmp_path, routed_llm):
    import skippy_memory

    store = str(tmp_path / "projects")
    routed_llm.load([finish("Added retry with backoff to the transport.", files=["calc/ops.py"])])
    outcome = await skippy_agent.run_task("Add retry", box, memory_root=store)

    assert outcome.session_id
    memory = skippy_memory.open_project(root=store, workspace_roots=list(box.roots))
    records = memory.sessions()
    assert len(records) == 1
    assert "backoff" in records[0]["summary"]
    assert records[0]["files_changed"] == ["calc/ops.py"]


@pytest.mark.asyncio
async def test_a_second_session_opens_knowing_what_the_first_did(box, tmp_path, routed_llm):
    """The whole point. Without this the second run re-derives everything and may
    redo work the first already finished."""
    store = str(tmp_path / "projects")

    routed_llm.load([finish("Added retry with backoff in the transport layer.")])
    await skippy_agent.run_task("Add retry", box, memory_root=store)

    second = skippy_agent.AgentLoop("Now add a circuit breaker", box, memory_root=store)
    opening = second.transcript.messages[1]["content"]
    assert "backoff in the transport" in opening


@pytest.mark.asyncio
async def test_a_run_that_ran_out_of_steps_is_still_handed_forward(box, tmp_path, routed_llm):
    """A half-finished migration is the most useful thing for the next session to
    know, and a save-on-success rule would have discarded exactly that.

    Recorded even though this run wrote nothing: unlike an unreachable endpoint, the
    model did run and call tools, so "this was attempted and did not get there" is
    real history about the task.
    """
    store = str(tmp_path / "projects")
    routed_llm.load([
        fl.tool_call("read_file", call_id="c1", path="calc/ops.py"),
        fl.tool_call("read_file", call_id="c2", path="README.md"),
    ])
    outcome = await skippy_agent.run_task("Big migration", box, max_steps=2, memory_root=store)
    assert outcome.status == "max_steps"

    opening = skippy_agent.AgentLoop("Continue", box, memory_root=store).transcript.messages[1]["content"]
    assert "max_steps" in opening
    assert "Big migration" in opening or "Ran out of steps" in opening


@pytest.mark.asyncio
async def test_a_decision_recorded_in_one_session_reaches_the_next(box, tmp_path, routed_llm):
    store = str(tmp_path / "projects")
    routed_llm.load([
        fl.tool_call(
            "record_decision", call_id="c1",
            title="Retries belong in the transport",
            body="Per-call retries duplicated the backoff logic in four places.",
            affects="calc/ops.py",
        ),
        finish("Moved retries into the transport.", call_id="c2"),
    ])
    outcome = await skippy_agent.run_task("Sort out retries", box, memory_root=store)
    assert outcome.status == "finished"

    opening = skippy_agent.AgentLoop("Something else", box, memory_root=store).transcript.messages[1]["content"]
    assert "Retries belong in the transport" in opening


@pytest.mark.asyncio
async def test_a_stale_decision_is_flagged_to_the_next_session(box, tmp_path, routed_llm, repo):
    """Confidently wrong memory is worse than none: unmarked, the next session works
    from a file that is no longer there."""
    store = str(tmp_path / "projects")
    routed_llm.load([
        fl.tool_call("record_decision", call_id="c1", title="Ops live in calc/ops.py",
                     body="Kept next to the transport.", affects="calc/ops.py"),
        finish(call_id="c2"),
    ])
    await skippy_agent.run_task("Decide", box, memory_root=store)

    (repo / "calc" / "ops.py").unlink()
    opening = skippy_agent.AgentLoop("Continue", box, memory_root=store).transcript.messages[1]["content"]
    assert "OUT OF DATE" in opening


def test_a_brand_new_project_adds_no_preamble(box, tmp_path):
    loop = skippy_agent.AgentLoop("First ever task", box, memory_root=str(tmp_path / "projects"))
    opening = loop.transcript.messages[1]["content"]
    assert "already know" not in opening


def test_memory_can_be_switched_off(box, tmp_path):
    loop = skippy_agent.AgentLoop("t", box, memory_root=str(tmp_path / "p"), remember=False)
    assert loop.memory is None


@pytest.mark.asyncio
async def test_a_run_survives_memory_being_unavailable(box, tmp_path, routed_llm, monkeypatch):
    """An unmounted NAS costs continuity, not the run."""
    import skippy_memory

    def unavailable(*args, **kwargs):
        raise OSError("memory root not mounted")

    monkeypatch.setattr(skippy_memory, "open_project", unavailable)
    routed_llm.load([finish("Done anyway.")])
    outcome = await skippy_agent.run_task("Do the thing", box)
    assert outcome.status == "finished"


@pytest.mark.asyncio
async def test_a_failure_to_write_the_record_does_not_fail_the_run(box, tmp_path, routed_llm):
    """The work is already on disk; losing the note about it must not lose the run."""
    class Broken:
        project_id = "broken"

        def opening_context(self):
            return ""

        def record_session(self, **kwargs):
            raise OSError("disk full")

    routed_llm.load([finish("Work completed.")])
    outcome = await skippy_agent.run_task("Do the thing", box, memory=Broken())
    assert outcome.status == "finished"
    assert outcome.session_id == ""


@pytest.mark.asyncio
async def test_the_project_is_not_model_controlled(box, tmp_path, routed_llm):
    """A model that could pick the project could read another one's history."""
    import skippy_dispatch
    import skippy_memory

    store = str(tmp_path / "projects")
    mine = skippy_memory.open_project(root=store, workspace_roots=list(box.roots))
    result = await skippy_dispatch.dispatch(
        "record_decision",
        {"title": "t", "body": "b", "memory": "somewhere-else"},
        box,
        memory=mine,
    )
    assert result.ok
    assert len(mine.decisions()) == 1


@pytest.mark.asyncio
async def test_the_memory_tools_report_plainly_when_there_is_no_memory(box, routed_llm):
    import skippy_dispatch

    result = await skippy_dispatch.dispatch("record_decision", {"title": "t", "body": "b"}, box)
    assert not result.ok
    assert "finish summary" in result.summary


def test_both_modes_can_remember(box, tmp_path):
    """Continuing prior work is not specific to coding or to RE."""
    for mode, extra in (("coding", {}), ("re", {"notes_root": str(tmp_path / "n")})):
        loop = skippy_agent.AgentLoop(
            "t", box, mode=mode, memory_root=str(tmp_path / "p"), **extra
        )
        offered = {t["function"]["name"] for t in loop.tools()}
        assert {"record_decision", "recall_project"} <= offered, mode


@pytest.mark.asyncio
async def test_an_unreachable_model_is_not_recorded_as_project_history(box, tmp_path, monkeypatch):
    """Found live: the endpoint was down, so the run failed at step zero and memory
    recorded "Model unavailable: RemoteProtocolError ..." as if it were something
    learned about the code. It is an ops event, and in a bounded context it displaces
    real history."""
    import skippy_llm
    import skippy_memory

    async def unreachable(*args, **kwargs):
        raise skippy_llm.ModelError("Role 'heavy' unreachable after 3 attempts.")

    monkeypatch.setattr(skippy_llm, "query_message", unreachable)
    store = str(tmp_path / "projects")
    outcome = await skippy_agent.run_task("Do the thing", box, memory_root=store)

    assert outcome.status == "failed"
    assert outcome.session_id == ""
    memory = skippy_memory.open_project(root=store, workspace_roots=list(box.roots))
    assert memory.sessions() == []
    assert memory.opening_context() == ""


@pytest.mark.asyncio
async def test_a_failed_run_that_touched_files_is_recorded(box, tmp_path, routed_llm, repo):
    """The other half: an aborted run that half-edited a file is exactly what the next
    session must know about, however it ended."""
    import skippy_memory

    store = str(tmp_path / "projects")
    routed_llm.load([
        fl.tool_call("apply_patch", call_id="c1", edits=[
            {"path": "calc/ops.py", "search": "a + b", "replace": "a + b  # half done"}
        ]),
        # Then nothing useful, until the step ceiling.
        fl.tool_call("read_file", call_id="c2", path="README.md"),
    ])
    outcome = await skippy_agent.run_task("Refactor", box, max_steps=2, memory_root=store)

    assert outcome.status == "max_steps"
    assert outcome.session_id
    memory = skippy_memory.open_project(root=store, workspace_roots=list(box.roots))
    assert "calc/ops.py" in memory.opening_context()


@pytest.mark.asyncio
async def test_a_finished_run_that_changed_nothing_is_still_recorded(box, tmp_path, routed_llm):
    """"Looked into this and there was nothing to change" saves the next session the
    same investigation."""
    import skippy_memory

    store = str(tmp_path / "projects")
    routed_llm.load([finish("Checked the retry path; it already handles this correctly.")])
    outcome = await skippy_agent.run_task("Check retries", box, memory_root=store)

    assert outcome.session_id
    memory = skippy_memory.open_project(root=store, workspace_roots=list(box.roots))
    assert "already handles this" in memory.opening_context()


@pytest.mark.asyncio
async def test_a_cancelled_run_that_produced_nothing_is_not_history(box, tmp_path, routed_llm):
    """Stopping a run immediately is not a fact about the project."""
    import skippy_memory

    store = str(tmp_path / "projects")
    loop = skippy_agent.AgentLoop("Do the thing", box, memory_root=store)
    loop.cancel()
    outcome = await loop.run()

    assert outcome.status == "cancelled"
    assert outcome.session_id == ""
    memory = skippy_memory.open_project(root=store, workspace_roots=list(box.roots))
    assert memory.sessions() == []
