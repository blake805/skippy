"""Arguments whose type disagrees with the schema become observations, not crashes.

The tool parser server-side falls back to the raw string when a structured parameter
fails to parse (the patched mlx_lm Qwen3-Coder parser — see test_tool_parser_patch.py),
so an array-typed parameter can arrive at dispatch as text. Before dispatch checked,
each tool defended itself or did not: apply_patch refused cleanly, but a str where a
list was expected can also iterate character by character, which is the failure that
looks like nothing until it corrupts something downstream.

Dispatch now checks every argument against the tool's own schema: unambiguous repairs
are made silently (a JSON array that arrived as a string, "300" where a number belongs),
anything else is refused with a message that names the argument and says how to re-send.
"""

import pytest

import skippy_agent
import skippy_dispatch
import skippy_edit
from skippy_sandbox import Sandbox
from tests import fake_llm as fl


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


# -- the live case: edits arrives as text ------------------------------------

@pytest.mark.asyncio
async def test_edits_as_unparseable_text_is_refused_with_directions(box):
    """The exact payload shape the parser fallback produces: model-written pseudo-repr
    that neither json.loads nor ast.literal_eval accepts."""
    poison = "[{'path': 'calc/ops.py', 'search': 'the motor's limit', 'replace': 'x'}]"
    result = await skippy_dispatch.dispatch("apply_patch", {"edits": poison}, box)

    assert not result.ok
    # The message must not just say "send a list" — the model believes it already did.
    assert "does not parse" in result.summary
    assert "JSON" in result.summary


@pytest.mark.asyncio
async def test_edits_as_a_valid_json_string_is_repaired_and_applied(box, repo):
    """When the string is valid JSON, the repair is unambiguous and costs no step."""
    edits = '[{"path": "calc/ops.py", "search": "a + b", "replace": "a - b"}]'
    result = await skippy_dispatch.dispatch("apply_patch", {"edits": edits}, box)

    assert result.ok, result.summary
    assert "a - b" in (repo / "calc" / "ops.py").read_text()


def test_apply_patch_itself_explains_a_string_edits(box):
    """Defense in depth: skippy_edit is also called directly, not only via dispatch."""
    result = skippy_edit.apply_patch(box, "not a list at all")
    assert not result.ok
    assert "did not parse" in result.summary


# -- scalar repairs and refusals ----------------------------------------------

@pytest.mark.asyncio
async def test_a_numeric_string_where_an_integer_belongs_is_coerced(box):
    result = await skippy_dispatch.dispatch(
        "read_file", {"path": "calc/ops.py", "start_line": "2", "end_line": "2"}, box
    )
    assert result.ok, result.summary
    assert "return a + b" in result.content


@pytest.mark.asyncio
async def test_a_non_numeric_integer_is_refused_by_name(box):
    """The same payload crashes the model server's handler thread unpatched; here it
    must come back as an observation naming the argument."""
    result = await skippy_dispatch.dispatch(
        "read_file", {"path": "calc/ops.py", "start_line": "all"}, box
    )
    assert not result.ok
    assert "start_line" in result.summary
    assert "integer" in result.summary


@pytest.mark.asyncio
async def test_a_boolean_string_is_coerced(box, repo):
    result = await skippy_dispatch.dispatch(
        "apply_patch",
        {
            "edits": [{"path": "calc/ops.py", "search": "a + b", "replace": "a - b"}],
            "dry_run": "true",
        },
        box,
    )
    assert result.ok, result.summary
    # dry_run held: the diff came back but nothing was written.
    assert "a + b" in (repo / "calc" / "ops.py").read_text()


# -- the loop's own array parameter -------------------------------------------

@pytest.mark.asyncio
async def test_finish_files_changed_as_text_does_not_become_single_characters(box, routed_llm):
    """finish is handled by the loop, not dispatch, so it needs its own guard: iterating
    a string fills files_changed with one-letter 'paths' that then flow into the outcome
    and project memory."""
    routed_llm.load([
        fl.tool_call("finish", summary="done", files_changed="calc/ops.py, README.md"),
    ])
    outcome = await skippy_agent.run_task("t", box)

    assert outcome.status == "finished"
    assert outcome.files_changed == []
