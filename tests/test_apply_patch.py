import os

import pytest

import skippy_agent_tools as agent_tools
from skippy_agent_tools import Sandbox, SandboxError, ToolContext


def read(root, relative):
    with open(os.path.join(root, relative), "r", encoding="utf-8") as handle:
        return handle.read()


def test_multi_file_patch_applies_every_edit(tool_ctx, sample_repo):
    result = agent_tools.apply_patch(
        tool_ctx,
        [
            {
                "path": "calc/ops.py",
                "action": "edit",
                "search": "def subtract(left: float, right: float) -> float:\n    return left - right",
                "replace": (
                    "def subtract(left: float, right: float) -> float:\n    return left - right\n\n\n"
                    "def divide(left: float, right: float) -> float:\n"
                    '    if right == 0:\n        raise ZeroDivisionError("right must be non-zero")\n'
                    "    return left / right"
                ),
            },
            {
                "path": "calc/__init__.py",
                "action": "edit",
                "search": 'from .ops import add, subtract\n\n__all__ = ["add", "subtract"]',
                "replace": 'from .ops import add, divide, subtract\n\n__all__ = ["add", "divide", "subtract"]',
            },
            {"path": "NOTES.md", "action": "create", "content": "# notes\n"},
        ],
    )

    assert result.ok, result.content
    assert "divide" in read(sample_repo, "calc/ops.py")
    assert "divide" in read(sample_repo, "calc/__init__.py")
    assert read(sample_repo, "NOTES.md") == "# notes\n"
    assert {report["path"] for report in result.data["files"]} == {
        "calc/ops.py",
        "calc/__init__.py",
        "NOTES.md",
    }
    assert result.data["diff"].startswith("--- a/calc/ops.py")


def test_a_single_bad_edit_writes_nothing(tool_ctx, sample_repo):
    before_ops = read(sample_repo, "calc/ops.py")

    result = agent_tools.apply_patch(
        tool_ctx,
        [
            {"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 0"},
            {"path": "calc/ops.py", "action": "edit", "search": "does not exist", "replace": "x"},
            {"path": "BRAND_NEW.md", "action": "create", "content": "nope"},
        ],
    )

    assert not result.ok
    assert "edit 1 (calc/ops.py)" in result.content
    assert "'search' text not found" in result.content
    assert read(sample_repo, "calc/ops.py") == before_ops
    assert not os.path.exists(os.path.join(sample_repo, "BRAND_NEW.md"))


def test_sequential_edits_to_one_file_stack(tool_ctx, sample_repo):
    result = agent_tools.apply_patch(
        tool_ctx,
        [
            {"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return left + right  # step1"},
            {"path": "calc/ops.py", "action": "edit", "search": "# step1", "replace": "# step2"},
        ],
    )

    assert result.ok, result.content
    assert "# step2" in read(sample_repo, "calc/ops.py")
    assert "# step1" not in read(sample_repo, "calc/ops.py")


def test_ambiguous_search_is_rejected_with_guidance(tool_ctx, sample_repo):
    agent_tools.apply_patch(
        tool_ctx, [{"path": "dup.py", "action": "create", "content": "x = 1\nx = 1\n"}]
    )

    result = agent_tools.apply_patch(
        tool_ctx, [{"path": "dup.py", "action": "edit", "search": "x = 1", "replace": "x = 2"}]
    )

    assert not result.ok
    assert "matched 2 times" in result.content
    assert read(sample_repo, "dup.py") == "x = 1\nx = 1\n"


def test_replace_all_and_occurrence_targeting(tool_ctx, sample_repo):
    agent_tools.apply_patch(
        tool_ctx, [{"path": "dup.py", "action": "create", "content": "a\na\na\n"}]
    )

    nth = agent_tools.apply_patch(
        tool_ctx, [{"path": "dup.py", "action": "edit", "search": "a", "replace": "B", "occurrence": 2}]
    )
    assert nth.ok, nth.content
    assert read(sample_repo, "dup.py") == "a\nB\na\n"

    everything = agent_tools.apply_patch(
        tool_ctx, [{"path": "dup.py", "action": "edit", "search": "a", "replace": "C", "replace_all": True}]
    )
    assert everything.ok, everything.content
    assert read(sample_repo, "dup.py") == "C\nB\nC\n"


def test_create_refuses_to_clobber_without_overwrite(tool_ctx, sample_repo):
    result = agent_tools.apply_patch(
        tool_ctx, [{"path": "calc/ops.py", "action": "create", "content": "wiped"}]
    )
    assert not result.ok
    assert "already exists" in result.content

    forced = agent_tools.apply_patch(
        tool_ctx,
        [{"path": "calc/ops.py", "action": "create", "content": "replaced\n", "overwrite": True}],
    )
    assert forced.ok, forced.content
    assert read(sample_repo, "calc/ops.py") == "replaced\n"


def test_delete_requires_an_existing_file(tool_ctx, sample_repo):
    missing = agent_tools.apply_patch(tool_ctx, [{"path": "ghost.py", "action": "delete"}])
    assert not missing.ok

    removed = agent_tools.apply_patch(tool_ctx, [{"path": "README.md", "action": "delete"}])
    assert removed.ok, removed.content
    assert not os.path.exists(os.path.join(sample_repo, "README.md"))


def test_dry_run_reports_a_diff_without_touching_disk(sandbox, sample_repo):
    ctx = ToolContext(sandbox=sandbox, dry_run=True)
    before = read(sample_repo, "calc/ops.py")

    result = agent_tools.apply_patch(
        ctx, [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}]
    )

    assert result.ok
    assert result.data["dry_run"] is True
    assert "return 42" in result.data["diff"]
    assert read(sample_repo, "calc/ops.py") == before


def test_backups_capture_the_pre_image(sandbox, sample_repo, tmp_path):
    backup_dir = str(tmp_path / "backups")
    ctx = ToolContext(sandbox=sandbox, backup_dir=backup_dir)
    before = read(sample_repo, "calc/ops.py")

    result = agent_tools.apply_patch(
        ctx, [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}]
    )

    assert result.ok
    saved = os.path.join(backup_dir, "calc__ops.py.orig")
    assert read(os.path.dirname(saved), os.path.basename(saved)) == before
    assert os.path.exists(os.path.join(backup_dir, "manifest.json"))


@pytest.mark.parametrize(
    "escape",
    ["../outside.py", "../../etc/passwd", "/etc/passwd", "calc/../../outside.py"],
)
def test_paths_outside_the_roots_are_refused(tool_ctx, escape):
    result = agent_tools.apply_patch(
        tool_ctx, [{"path": escape, "action": "create", "content": "pwned"}]
    )
    assert not result.ok
    assert "outside the workspace roots" in result.content


def test_symlinked_escape_is_refused(sample_repo):
    outside = os.path.join(os.path.dirname(sample_repo), "outside")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as handle:
        handle.write("classified")
    os.symlink(outside, os.path.join(sample_repo, "escape"))

    ctx = ToolContext(sandbox=Sandbox([sample_repo]))
    with pytest.raises(SandboxError):
        ctx.sandbox.resolve("escape/secret.txt")

    result = agent_tools.apply_patch(
        ctx, [{"path": "escape/secret.txt", "action": "create", "content": "x", "overwrite": True}]
    )
    assert not result.ok
    assert read(outside, "secret.txt") == "classified"


def test_noop_patch_is_reported_as_such(tool_ctx):
    result = agent_tools.apply_patch(
        tool_ctx,
        [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return left + right"}],
    )
    assert result.ok
    assert "no-op" in result.summary
