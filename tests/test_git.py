"""The git tool belt: branch, status, diff, commit.

The contract under test is the one stated in skippy_git's docstring: reads are
free, a commit shows the human the exact staged diff and a deny leaves the tree
untouched, and switching branches is refused while anything uncommitted could
be clobbered.
"""

import os
import subprocess

import pytest

import skippy_git
from skippy_sandbox import Sandbox, ToolResult


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


def make_repo(path, initial=True):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    if initial:
        (path / "README.md").write_text("# a repo\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "proj")


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


class StubApprover:
    """Stands in for skippy_cursor.CodeApprover: records the card, answers."""

    def __init__(self, answer="APPROVE"):
        self.answer = answer
        self.cards = []

    async def approve(self, summary, diff, files):
        self.cards.append({"summary": summary, "diff": diff, "files": files})
        if self.answer == "APPROVE":
            return None
        return ToolResult(False, "you declined the change in the app")


# -- status -------------------------------------------------------------


async def test_status_on_a_clean_repo_names_the_branch_and_last_commit(box):
    result = await skippy_git.git_status(box)
    assert result.ok
    assert result.data["branch"] == "main"
    assert result.data["changes"] == []
    assert result.data["last_commit"]["subject"] == "initial"
    assert "clean" in result.content.lower()


async def test_status_reports_modified_and_untracked_files(box, repo):
    (repo / "README.md").write_text("changed\n")
    (repo / "new.py").write_text("x = 1\n")
    result = await skippy_git.git_status(box)
    assert result.ok
    statuses = {c["path"]: c["status"] for c in result.data["changes"]}
    assert statuses["README.md"] == "M"
    assert statuses["new.py"] == "??"


async def test_a_repo_outside_the_roots_is_refused(box, tmp_path):
    outside = make_repo(tmp_path / "elsewhere")
    result = await skippy_git.git_status(box, repo=str(outside))
    assert not result.ok
    assert "sandbox" in result.summary.lower()


async def test_several_repos_require_the_repo_argument(tmp_path):
    a = make_repo(tmp_path / "a")
    b = make_repo(tmp_path / "b")
    box = Sandbox([str(a), str(b)])
    result = await skippy_git.git_status(box)
    assert not result.ok
    assert "'repo' is required" in result.summary
    assert "a" in result.summary and "b" in result.summary

    named = await skippy_git.git_status(box, repo="a")
    assert named.ok


async def test_a_root_that_is_not_a_repo_says_so(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    box = Sandbox([str(plain)])
    result = await skippy_git.git_status(box)
    assert not result.ok
    assert "not" in result.summary.lower() and "repo" in result.summary.lower()


# -- diff ---------------------------------------------------------------


async def test_diff_shows_the_change_and_names_untracked_files(box, repo):
    (repo / "README.md").write_text("# a repo\nmore\n")
    (repo / "loose.txt").write_text("untracked\n")
    result = await skippy_git.git_diff(box)
    assert result.ok
    assert "+more" in result.content
    assert result.data["untracked"] == ["loose.txt"]


async def test_staged_diff_shows_only_what_is_staged(box, repo):
    (repo / "README.md").write_text("staged change\n")
    _git(repo, "add", "README.md")
    (repo / "README.md").write_text("staged change\nplus an unstaged one\n")

    staged = await skippy_git.git_diff(box, staged=True)
    assert "+staged change" in staged.content
    assert "unstaged one" not in staged.content

    working = await skippy_git.git_diff(box)
    assert "+plus an unstaged one" in working.content


async def test_diff_scoped_to_a_path_stays_inside_the_repo(box, repo, tmp_path):
    (repo / "README.md").write_text("changed\n")
    scoped = await skippy_git.git_diff(box, path="README.md")
    assert scoped.ok
    assert "changed" in scoped.content


# -- branch -------------------------------------------------------------


async def test_branch_with_no_name_lists_branches(box):
    result = await skippy_git.git_branch(box)
    assert result.ok
    assert result.data["current"] == "main"
    assert "main" in result.data["branches"]


async def test_creating_a_branch_switches_to_it(box, repo):
    result = await skippy_git.git_branch(box, name="feature/panel", create=True)
    assert result.ok
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "feature/panel"


async def test_switching_with_uncommitted_changes_is_refused(box, repo):
    _git(repo, "branch", "other")
    (repo / "README.md").write_text("dirty\n")
    result = await skippy_git.git_branch(box, name="other")
    assert not result.ok
    assert "uncommitted" in result.summary.lower()
    # And nothing moved.
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "main"


async def test_switching_a_clean_tree_works(box, repo):
    _git(repo, "branch", "other")
    result = await skippy_git.git_branch(box, name="other")
    assert result.ok
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "other"


async def test_a_bad_branch_name_is_refused(box):
    result = await skippy_git.git_branch(box, name="not a branch", create=True)
    assert not result.ok
    assert "not a valid branch name" in result.summary


# -- commit -------------------------------------------------------------


async def test_commit_without_an_approver_commits_everything(box, repo):
    (repo / "README.md").write_text("changed\n")
    (repo / "new.py").write_text("x = 1\n")
    result = await skippy_git.git_commit(box, "add the thing")
    assert result.ok
    assert result.data["branch"] == "main"
    assert result.data["commit"]
    assert {f["path"] for f in result.data["committed"]} == {"README.md", "new.py"}
    # The tree is clean afterwards.
    status = await skippy_git.git_status(box)
    assert status.data["changes"] == []
    assert status.data["last_commit"]["subject"] == "add the thing"


async def test_commit_scoped_to_paths_leaves_the_rest_alone(box, repo):
    (repo / "included.py").write_text("a\n")
    (repo / "excluded.py").write_text("b\n")
    result = await skippy_git.git_commit(box, "only one file", paths=["included.py"])
    assert result.ok
    assert [f["path"] for f in result.data["committed"]] == ["included.py"]
    status = await skippy_git.git_status(box)
    assert [c["path"] for c in status.data["changes"]] == ["excluded.py"]


async def test_the_approval_card_carries_the_message_and_the_staged_diff(box, repo):
    (repo / "README.md").write_text("changed\n")
    approver = StubApprover("APPROVE")
    result = await skippy_git.git_commit(box, "explain the why", approver=approver)
    assert result.ok
    card = approver.cards[0]
    assert "explain the why" in card["summary"]
    assert "+changed" in card["diff"]
    assert card["files"][0]["path"] == "README.md"


async def test_a_denied_commit_leaves_the_tree_exactly_as_found(box, repo):
    (repo / "README.md").write_text("changed\n")
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = await skippy_git.git_commit(box, "nope", approver=StubApprover("DENY"))
    assert not result.ok
    assert result.data.get("declined") is True
    # No commit was made and nothing is left staged.
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip()
    assert staged == ""
    # The working-tree change itself survives.
    assert (repo / "README.md").read_text() == "changed\n"


async def test_a_commit_with_nothing_to_commit_says_so(box):
    result = await skippy_git.git_commit(box, "empty")
    assert not result.ok
    assert "nothing to commit" in result.summary.lower()


async def test_a_commit_needs_a_message(box, repo):
    (repo / "README.md").write_text("changed\n")
    result = await skippy_git.git_commit(box, "")
    assert not result.ok
    assert "message" in result.summary.lower()


async def test_a_repo_with_no_identity_falls_back_rather_than_failing(tmp_path, monkeypatch):
    repo = tmp_path / "anon"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    # No local identity, and HOME pointed away from any global gitconfig.
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    (repo / "a.txt").write_text("y\n")
    box = Sandbox([str(repo)])
    result = await skippy_git.git_commit(box, "fallback identity")
    assert result.ok, result.summary


async def test_commit_paths_outside_the_repo_are_refused(box, tmp_path):
    result = await skippy_git.git_commit(box, "bad path", paths=[str(tmp_path / "elsewhere.txt")])
    assert not result.ok


# -- the repo list helper -----------------------------------------------


def test_list_repos_names_only_roots_that_are_repositories(tmp_path):
    a = make_repo(tmp_path / "a")
    plain = tmp_path / "plain"
    plain.mkdir()
    box = Sandbox([str(a), str(plain)])
    repos = skippy_git.list_repos(box)
    assert [r["name"] for r in repos] == ["a"]
    assert repos[0]["path"] == os.path.realpath(str(a))
