"""Tests for the test harness itself.

A harness that quietly lies makes every suite built on it worthless, and these
two in particular are load-bearing: `prefix_broken_at` is how the agent loop's
append-only invariant gets enforced in slice 7, and the leaked-XML helper has to
produce output the real recovery code actually parses.
"""

import pytest

import skippy_llm
from tests import fake_llm as fl


async def test_scripted_reply_is_returned_in_order(routed_llm):
    routed_llm.load([fl.text("first"), fl.text("second")])

    assert await skippy_llm.query_text([{"role": "user", "content": "a"}]) == "first"
    assert await skippy_llm.query_text([{"role": "user", "content": "b"}]) == "second"
    assert routed_llm.remaining == 0


async def test_native_tool_call_survives_the_round_trip(routed_llm):
    routed_llm.load([fl.tool_call("read_file", thought="looking", path="a.py")])

    message = await skippy_llm.query_message(
        [{"role": "user", "content": "go"}], tools=[{"function": {"name": "read_file"}}]
    )
    assert message["content"] == "looking"
    assert message["tool_calls"][0]["name"] == "read_file"
    assert message["tool_calls"][0]["arguments"] == {"path": "a.py"}


async def test_leaked_xml_helper_produces_output_the_real_parser_recovers(routed_llm):
    # If this helper and parse_leaked_tool_calls ever disagree, every test using
    # the helper would be exercising a format the model never actually emits.
    routed_llm.load([fl.leaked_tool_call("run_tests", thought="ok ", path="tests/")])

    message = await skippy_llm.query_message(
        [{"role": "user", "content": "go"}], tools=[{"function": {"name": "run_tests"}}]
    )
    assert message["tool_calls"][0]["name"] == "run_tests"
    assert message["tool_calls"][0]["arguments"] == {"path": "tests/"}
    assert message["content"] == "ok"


async def test_malformed_arguments_reach_the_caller_as_malformed(routed_llm):
    routed_llm.load([fl.malformed_tool_call("patch_file")])

    message = await skippy_llm.query_message(
        [{"role": "user", "content": "go"}], tools=[{"function": {"name": "patch_file"}}]
    )
    assert "_malformed_arguments" in message["tool_calls"][0]["arguments"]


async def test_http_error_reply_makes_the_client_raise(routed_llm):
    routed_llm.load([fl.http_error(503)])

    with pytest.raises(skippy_llm.ModelError) as exc:
        await skippy_llm.query_message([{"role": "user", "content": "go"}], attempts=1)
    assert "503" in str(exc.value)


async def test_compression_calls_do_not_consume_the_script(routed_llm):
    routed_llm.load([fl.text("the real turn")])

    summary = await skippy_llm.compress("a big pile of bytes", instruction="what matters")
    assert summary == "[compressed summary]"
    # The scripted turn is still queued: compression is infrastructure.
    assert routed_llm.remaining == 1
    assert await skippy_llm.query_text([{"role": "user", "content": "go"}]) == "the real turn"


async def test_tools_offered_reports_the_schemas_sent(routed_llm):
    routed_llm.load([fl.text("ok")])
    await skippy_llm.query_message(
        [{"role": "user", "content": "go"}],
        tools=[{"function": {"name": "read_file"}}, {"function": {"name": "run_tests"}}],
    )
    assert routed_llm.tools_offered() == ["read_file", "run_tests"]


async def test_observations_collects_tool_results_in_order(routed_llm):
    routed_llm.load([fl.text("one"), fl.text("two")])
    await skippy_llm.query_text([{"role": "user", "content": "go"}])
    await skippy_llm.query_text([
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
    ])
    assert routed_llm.observations() == ["file contents"]


# --- the append-only detector ---

async def test_prefix_detector_passes_when_every_request_only_appends(routed_llm):
    routed_llm.load([fl.text("a"), fl.text("b"), fl.text("c")])
    transcript = skippy_llm.Transcript(system="sys")

    for turn in ("one", "two", "three"):
        transcript.append({"role": "user", "content": turn})
        await skippy_llm.query_text(transcript.messages)

    assert routed_llm.prefix_broken_at() is None


async def test_prefix_detector_catches_a_deletion_from_the_middle(routed_llm):
    # This is exactly the `del self.messages[2:4]` defect from lineage B: the
    # detector has to notice it, or the invariant is unguarded.
    routed_llm.load([fl.text("a"), fl.text("b")])

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    await skippy_llm.query_text(messages)
    del messages[1:3]
    messages.append({"role": "user", "content": "four"})
    await skippy_llm.query_text(messages)

    assert routed_llm.prefix_broken_at() == 1


async def test_prefix_detector_catches_an_edit_to_an_already_sent_turn(routed_llm):
    routed_llm.load([fl.text("a"), fl.text("b")])

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "one"}]
    await skippy_llm.query_text(messages)
    messages[1] = {"role": "user", "content": "one, revised"}
    messages.append({"role": "user", "content": "two"})
    await skippy_llm.query_text(messages)

    assert routed_llm.prefix_broken_at() == 1


async def test_a_single_request_can_never_break_the_prefix(routed_llm):
    routed_llm.load([fl.text("a")])
    await skippy_llm.query_text([{"role": "user", "content": "one"}])
    assert routed_llm.prefix_broken_at() is None
