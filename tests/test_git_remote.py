"""Remote git verbs and the GitHub connection, without any network.

The remote in these tests is a bare repository on disk: git treats a path
exactly as it treats a URL, so push/pull round-trips, rejections and
divergence are all exercised for real — only api.github.com is faked.
The token store is pointed at a tmp dir via SKIPPY_CONFIG_DIR, so the
suite never touches a real ~/.skippy.
"""

import os
import stat
import subprocess

import pytest

import skippy_git
import skippy_github
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


def make_bare(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "--bare", "-b", "main")
    return path


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    monkeypatch.setenv("SKIPPY_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def repo(tmp_path, config_dir):
    return make_repo(tmp_path / "proj")


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


@pytest.fixture
def remote(tmp_path, repo):
    """A bare 'origin' the repo is wired to, with main already pushed."""
    bare = make_bare(tmp_path / "origin.git")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return bare


class StubApprover:
    def __init__(self, answer="APPROVE"):
        self.answer = answer
        self.cards = []

    async def approve(self, summary, diff, files):
        self.cards.append({"summary": summary, "diff": diff, "files": files})
        if self.answer == "APPROVE":
            return None
        return ToolResult(False, "you declined the change in the app")


# -- push ---------------------------------------------------------------


async def test_push_sends_new_commits_to_origin(box, repo, remote):
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add a")

    result = await skippy_git.git_push(box)
    assert result.ok
    assert result.data["pushed"] is True
    assert result.data["branch"] == "main"

    log = subprocess.run(
        ["git", "log", "-1", "--format=%s", "main"],
        cwd=remote, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "add a"


async def test_push_without_an_upstream_sets_one(box, repo, tmp_path):
    bare = make_bare(tmp_path / "fresh.git")
    _git(repo, "remote", "add", "origin", str(bare))

    result = await skippy_git.git_push(box)
    assert result.ok
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "main@{upstream}"],
        cwd=repo, capture_output=True, text=True,
    )
    assert upstream.stdout.strip() == "origin/main"


async def test_push_without_origin_names_the_problem(box):
    result = await skippy_git.git_push(box)
    assert not result.ok
    assert "origin" in result.summary


async def test_rejected_push_suggests_pulling(box, repo, remote, tmp_path):
    # Someone else pushed first: a second clone advances origin/main.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), "other")
    (other / "theirs.txt").write_text("theirs\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "their change")
    _git(other, "push", "-q", "origin", "main")

    (repo / "mine.txt").write_text("mine\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "my change")

    result = await skippy_git.git_push(box)
    assert not result.ok
    assert "pull" in result.summary.lower()


async def test_an_approved_push_shows_the_outgoing_commits(box, repo, remote):
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the outgoing commit")

    approver = StubApprover("APPROVE")
    result = await skippy_git.git_push(box, approver=approver)
    assert result.ok
    assert len(approver.cards) == 1
    assert "the outgoing commit" in approver.cards[0]["diff"]


async def test_a_declined_push_pushes_nothing(box, repo, remote):
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "held back")

    result = await skippy_git.git_push(box, approver=StubApprover("DENY"))
    assert not result.ok
    assert result.data.get("declined") is True
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s", "main"],
        cwd=remote, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "initial"


# -- pull ---------------------------------------------------------------


async def test_pull_fast_forwards_from_origin(box, repo, remote, tmp_path):
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), "other")
    (other / "news.txt").write_text("news\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote change")
    _git(other, "push", "-q", "origin", "main")

    result = await skippy_git.git_pull(box)
    assert result.ok
    assert (repo / "news.txt").exists()


async def test_pull_when_up_to_date_says_so(box, repo, remote):
    result = await skippy_git.git_pull(box)
    assert result.ok
    assert result.data["up_to_date"] is True


async def test_a_diverged_branch_refuses_to_pull_and_names_the_fix(box, repo, remote, tmp_path):
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), "other")
    (other / "theirs.txt").write_text("theirs\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "their change")
    _git(other, "push", "-q", "origin", "main")

    (repo / "mine.txt").write_text("mine\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "my change")

    result = await skippy_git.git_pull(box)
    assert not result.ok
    assert "diverged" in result.summary.lower()
    # No surprise merge: local history still has exactly the local commit on top.
    log = _git(repo, "log", "--format=%s")
    assert "their change" not in log.stdout


async def test_a_declined_pull_pulls_nothing(box, repo, remote, tmp_path):
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), "other")
    (other / "news.txt").write_text("news\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote change")
    _git(other, "push", "-q", "origin", "main")

    result = await skippy_git.git_pull(box, approver=StubApprover("DENY"))
    assert not result.ok
    assert not (repo / "news.txt").exists()


# -- git_new ------------------------------------------------------------


async def test_new_repo_without_a_token_is_local_only(tmp_path, config_dir):
    home = tmp_path / "workspace"
    home.mkdir()
    box = Sandbox([str(home)])

    result = await skippy_git.git_new(box, "gadget")
    assert result.ok
    assert result.data["github"] is False
    assert (home / "gadget" / ".git").is_dir()
    assert (home / "gadget" / "README.md").exists()
    assert "token" in result.summary.lower()

    # And it appears in the repo list, one level under the root.
    names = [r["name"] for r in skippy_git.list_repos(box)]
    assert "gadget" in names


async def test_new_repo_with_a_faked_github_wires_origin_and_pushes(tmp_path, config_dir, monkeypatch):
    home = tmp_path / "workspace"
    home.mkdir()
    box = Sandbox([str(home)])
    bare = make_bare(tmp_path / "hub.git")

    async def fake_create(name, private=True, description=""):
        return {
            "full_name": f"tester/{name}", "clone_url": str(bare),
            "html_url": f"https://github.com/tester/{name}", "private": private,
        }

    monkeypatch.setattr(skippy_github, "get_token", lambda: "tok")
    monkeypatch.setattr(skippy_github, "create_repo", fake_create)

    result = await skippy_git.git_new(box, "gadget", private=True)
    assert result.ok
    assert result.data["github"] is True
    assert result.data["full_name"] == "tester/gadget"

    log = subprocess.run(
        ["git", "log", "-1", "--format=%s", "main"],
        cwd=bare, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "Initial commit"


async def test_new_repo_survives_a_github_refusal(tmp_path, config_dir, monkeypatch):
    home = tmp_path / "workspace"
    home.mkdir()
    box = Sandbox([str(home)])

    async def refuse(name, private=True, description=""):
        raise skippy_github.GitHubError("name already exists on this account")

    monkeypatch.setattr(skippy_github, "get_token", lambda: "tok")
    monkeypatch.setattr(skippy_github, "create_repo", refuse)

    result = await skippy_git.git_new(box, "gadget")
    assert result.ok  # the local half stands
    assert result.data["github"] is False
    assert "github said no" in result.summary.lower()
    assert (home / "gadget" / ".git").is_dir()


async def test_bad_repo_names_are_refused(tmp_path, config_dir):
    home = tmp_path / "workspace"
    home.mkdir()
    box = Sandbox([str(home)])
    for bad in ("", "../escape", "has space", "-leading"):
        result = await skippy_git.git_new(box, bad)
        assert not result.ok, bad
    assert not any(home.iterdir())


async def test_an_existing_directory_is_not_clobbered(tmp_path, config_dir):
    home = tmp_path / "workspace"
    (home / "gadget").mkdir(parents=True)
    box = Sandbox([str(home)])
    result = await skippy_git.git_new(box, "gadget")
    assert not result.ok
    assert "already exists" in result.summary


# -- git_clone ----------------------------------------------------------


async def test_clone_refuses_a_malformed_name(tmp_path, config_dir):
    home = tmp_path / "workspace"
    home.mkdir()
    box = Sandbox([str(home)])
    for bad in ("", "justname", "a/b/c", "../x/../y"):
        result = await skippy_git.git_clone(box, bad)
        assert not result.ok, bad
        assert "owner/name" in result.summary


async def test_clone_refuses_to_overwrite(tmp_path, config_dir):
    home = tmp_path / "workspace"
    (home / "thing").mkdir(parents=True)
    box = Sandbox([str(home)])
    result = await skippy_git.git_clone(box, "someone/thing")
    assert not result.ok
    assert "already exists" in result.summary


# -- list_repos one level deep -------------------------------------------


def test_list_repos_sees_roots_and_their_children(tmp_path):
    root = make_repo(tmp_path / "root")
    make_repo(tmp_path / "root" / "nested")
    (tmp_path / "root" / "plain").mkdir()  # not a repo; must not appear
    box = Sandbox([str(root)])

    names = sorted(r["name"] for r in skippy_git.list_repos(box))
    assert names == ["nested", "root"]


# -- the token store ------------------------------------------------------


def test_token_round_trip_and_permissions(config_dir):
    assert skippy_github.get_token() == ""
    skippy_github.set_token("  ghp_secret123  ")
    assert skippy_github.get_token() == "ghp_secret123"

    token_mode = stat.S_IMODE(os.stat(config_dir / "github_token").st_mode)
    assert token_mode == 0o600
    askpass = config_dir / "github_askpass.sh"
    assert os.access(askpass, os.X_OK)
    # The script reads the token file; the token itself is not in the script.
    assert "ghp_secret123" not in askpass.read_text()

    env = skippy_github.git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == str(askpass)


def test_clearing_the_token_removes_both_files(config_dir):
    skippy_github.set_token("ghp_x")
    skippy_github.set_token("")
    assert skippy_github.get_token() == ""
    assert not (config_dir / "github_token").exists()
    assert not (config_dir / "github_askpass.sh").exists()
    assert "GIT_ASKPASS" not in skippy_github.git_env()


def test_the_askpass_script_answers_git_prompts(config_dir):
    skippy_github.set_token("ghp_live")
    askpass = str(config_dir / "github_askpass.sh")
    user = subprocess.run([askpass, "Username for 'https://github.com':"],
                          capture_output=True, text=True, check=True)
    password = subprocess.run([askpass, "Password for 'https://github.com':"],
                              capture_output=True, text=True, check=True)
    assert user.stdout.strip() == "x-access-token"
    assert password.stdout.strip() == "ghp_live"
