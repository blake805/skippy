"""Git as a first-class tool surface: branch, status, diff, commit.

Until now the agent's only view of version control was the read-only slice of
`run_command`'s allowlist — enough to look at history, structurally unable to
record any. The write path exists here instead, as named tools with their own
contracts, for the same reason `apply_patch` is not "run `patch` yourself":
a structured tool can show a human exactly what is about to happen and refuse
the call cleanly when it should not happen at all.

Three decisions, stated plainly.

**A commit is a write, so it asks.** `git_commit` routes through the same
approval gate as code edits (ADR 0005's channel, `skippy_cursor.CodeApprover`):
the card carries the commit message and the exact staged diff, and nothing
lands in history without an approve. Deny unstages everything, leaving the
working tree as it was. When approvals are off (`SKIPPY_CODE_APPROVAL=off`) or
the run is headless, commits apply directly — the same posture as edits.

**Branches are cheap; losing work is not.** Creating a branch changes no file,
so it is free. Switching to an *existing* branch rewrites the working tree, so
it is only allowed when `git status` is clean — there is then nothing that can
be clobbered, and no approval card is needed for an operation that cannot
destroy anything.

**Repos are found through the sandbox.** Every repo argument resolves through
`Sandbox.resolve` and `Sandbox.repo_root_for`, so the tool surface can reach
exactly the repositories inside the workspace roots and nothing else — the
same boundary every other filesystem tool honours.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import re

import skippy_exec
import skippy_github
from skippy_sandbox import (
    MAX_TOOL_OUTPUT_CHARS,
    Sandbox,
    SandboxError,
    ToolResult,
    cap_text,
)

logger = logging.getLogger("skippy_git")

GIT_TIMEOUT = 60.0

# Used only when the repo has no identity configured: a commit that fails with
# "tell me who you are" is a dead end the model cannot fix, and inventing a
# fake human would be worse than being honest about the author.
FALLBACK_IDENTITY = ("Skippy", "skippy@hub.local")


async def _git(
    repo: str, *args: str,
    timeout: float = GIT_TIMEOUT,
    env_extra: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    """Run one git command in `repo`. Returns (exit_code, combined_output).

    `env_extra` exists for the remote verbs: skippy_github.git_env() rides in
    there so push/pull/clone can authenticate without any repo carrying a
    token in its config.
    """
    env = skippy_exec._child_env()
    if env_extra:
        env = {**env, **env_extra}
    try:
        process = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "git is not installed on this machine."
    except OSError as exc:
        return 126, f"Could not start git: {exc}"

    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return 124, f"git {' '.join(args)} timed out after {timeout:g}s."
    return process.returncode or 0, output.decode("utf-8", errors="replace")


def _locate_repo(sandbox: Sandbox, repo: Optional[str]) -> Tuple[Optional[str], Optional[ToolResult]]:
    """The repo root a tool call means, or a ToolResult explaining why not."""
    if repo:
        try:
            resolved = sandbox.resolve(repo, must_exist=True)
        except SandboxError as exc:
            return None, ToolResult(False, f"Sandbox violation: {exc}")
        root = sandbox.repo_root_for(resolved)
        if root is None:
            return None, ToolResult(
                False,
                f"'{repo}' is not inside a git repository. "
                f"Known repos: {', '.join(r['name'] for r in list_repos(sandbox)) or 'none'}.",
            )
        return root, None

    repos = list_repos(sandbox)
    if len(repos) == 1:
        return repos[0]["path"], None
    if not repos:
        return None, ToolResult(
            False,
            "No workspace root is a git repository. There is no repository to find — do "
            "not go looking for one with shell commands or by listing directories; this "
            "workspace simply is not under version control. Proceed without git.",
        )
    return None, ToolResult(
        False,
        "There are several repositories, so 'repo' is required. Repos: "
        + ", ".join(r["name"] for r in repos),
    )


def list_repos(sandbox: Sandbox) -> List[Dict[str, str]]:
    """Git repositories the sandbox can reach: each workspace root that is one,
    plus each root's immediate children that are.

    One level deep, not a recursive crawl: `git_new` and `git_clone` put new
    repositories directly under a root, so this is exactly the set a user can
    have created through the panel — without the cost or surprise of a scan
    that wanders into node_modules.
    """
    found: List[Dict[str, str]] = []
    seen: set = set()

    def add(path: str) -> None:
        if path in seen or not os.path.isdir(os.path.join(path, ".git")):
            return
        seen.add(path)
        name = os.path.basename(path)
        if any(entry["name"] == name for entry in found):
            name = sandbox.relative(path)
        found.append({"name": name, "path": path})

    for root in sandbox.roots:
        add(root)
        try:
            children = sorted(os.listdir(root))
        except OSError:
            continue
        for child in children:
            if child.startswith("."):
                continue
            candidate = os.path.join(root, child)
            if os.path.isdir(candidate):
                add(candidate)
    return found


def _parse_porcelain(output: str) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Parse `git status --porcelain=v1 -b` into branch info and change rows."""
    branch: Dict[str, Any] = {"branch": "", "ahead": 0, "behind": 0}
    changes: List[Dict[str, str]] = []
    for line in output.splitlines():
        if line.startswith("## "):
            head = line[3:]
            if "..." in head:
                head, _, rest = head.partition("...")
                for token in ("ahead ", "behind "):
                    if token in rest:
                        try:
                            value = int(rest.split(token, 1)[1].split("]")[0].split(",")[0])
                        except ValueError:
                            value = 0
                        branch["ahead" if token == "ahead " else "behind"] = value
            # A fresh repo reports "No commits yet on main".
            branch["branch"] = head.replace("No commits yet on ", "").split(" ")[0]
            continue
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        # Renames come as "old -> new"; the new name is the one that exists.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changes.append({"status": status.strip() or status, "path": path.strip('"')})
    return branch, changes


async def _last_commit(repo: str) -> Dict[str, str]:
    code, out = await _git(repo, "log", "-1", "--format=%h%x00%s%x00%cr%x00%an")
    if code != 0:
        return {}
    parts = out.strip().split("\x00")
    if len(parts) < 4:
        return {}
    return {"hash": parts[0], "subject": parts[1], "when": parts[2], "author": parts[3]}


def _describe_changes(changes: Sequence[Dict[str, str]]) -> str:
    if not changes:
        return "The working tree is clean."
    return "\n".join(f"{c['status']:>2}  {c['path']}" for c in changes)


# -- tools ------------------------------------------------------------------


async def git_status(sandbox: Sandbox, repo: Optional[str] = None) -> ToolResult:
    """Current branch, ahead/behind, and every changed file."""
    root, problem = _locate_repo(sandbox, repo)
    if problem:
        return problem

    code, out = await _git(root, "status", "--porcelain=v1", "-b")
    if code != 0:
        return ToolResult(False, f"git status failed in {sandbox.relative(root)}: {out.strip()}")

    branch, changes = _parse_porcelain(out)
    last = await _last_commit(root)
    name = sandbox.relative(root)
    summary = (
        f"{name}: on branch {branch['branch'] or '(unborn)'}, "
        f"{len(changes)} changed file(s)."
    )
    if branch["ahead"] or branch["behind"]:
        summary += f" Ahead {branch['ahead']}, behind {branch['behind']} of upstream."
    content = _describe_changes(changes)
    if last:
        content += f"\n\nLast commit: {last['hash']} {last['subject']} ({last['when']}, {last['author']})"
    return ToolResult(
        True, summary, content,
        {
            "repo": name, "branch": branch["branch"], "ahead": branch["ahead"],
            "behind": branch["behind"], "changes": changes, "last_commit": last,
        },
    )


async def git_diff(
    sandbox: Sandbox,
    repo: Optional[str] = None,
    path: Optional[str] = None,
    staged: bool = False,
) -> ToolResult:
    """Unified diff of the working tree (or the index, with staged=True)."""
    root, problem = _locate_repo(sandbox, repo)
    if problem:
        return problem

    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if path:
        try:
            resolved = sandbox.resolve(path)
        except SandboxError as exc:
            return ToolResult(False, f"Sandbox violation: {exc}")
        if not (resolved == root or resolved.startswith(root + os.sep)):
            return ToolResult(False, f"'{path}' is not inside the repo {sandbox.relative(root)}.")
        args += ["--", os.path.relpath(resolved, root)]

    code, out = await _git(root, *args)
    if code != 0:
        return ToolResult(False, f"git diff failed in {sandbox.relative(root)}: {out.strip()}")

    # Untracked files never appear in `git diff`; say so rather than letting an
    # empty diff imply there is nothing new.
    _, status_out = await _git(root, "status", "--porcelain=v1")
    untracked = [
        line[3:] for line in status_out.splitlines() if line.startswith("?? ")
    ]

    lane = "staged" if staged else "unstaged"
    if not out.strip():
        summary = f"No {lane} changes in {sandbox.relative(root)}."
        if untracked and not staged:
            summary += f" {len(untracked)} untracked file(s) exist; git diff does not show those."
        return ToolResult(True, summary, "", {"repo": sandbox.relative(root), "untracked": untracked})

    return ToolResult(
        True,
        f"{lane.capitalize()} diff in {sandbox.relative(root)}.",
        cap_text(out, MAX_TOOL_OUTPUT_CHARS),
        {"repo": sandbox.relative(root), "untracked": untracked},
    )


async def git_branch(
    sandbox: Sandbox,
    repo: Optional[str] = None,
    name: Optional[str] = None,
    create: bool = False,
) -> ToolResult:
    """List branches; create-and-switch a new one; or switch to a clean existing one."""
    root, problem = _locate_repo(sandbox, repo)
    if problem:
        return problem
    display = sandbox.relative(root)

    if not name:
        code, out = await _git(root, "branch", "--list", "--no-color")
        if code != 0:
            return ToolResult(False, f"git branch failed in {display}: {out.strip()}")
        branches, current = [], ""
        for line in out.splitlines():
            entry = line[2:].strip()
            if not entry:
                continue
            branches.append(entry)
            if line.startswith("* "):
                current = entry
        return ToolResult(
            True,
            f"{display}: {len(branches)} branch(es), on {current or '(unborn)'}.",
            "\n".join(branches),
            {"repo": display, "branches": branches, "current": current},
        )

    name = str(name).strip()
    code, out = await _git(root, "check-ref-format", "--branch", name)
    if code != 0:
        return ToolResult(False, f"'{name}' is not a valid branch name.")

    if create:
        code, out = await _git(root, "switch", "-c", name)
        if code != 0:
            return ToolResult(False, f"Could not create branch '{name}': {out.strip()}")
        return ToolResult(
            True, f"Created and switched to '{name}' in {display}.",
            data={"repo": display, "branch": name, "created": True},
        )

    # Switching rewrites the working tree, so it is only allowed when nothing
    # can be lost. A dirty tree gets a refusal that names the way forward.
    _, status_out = await _git(root, "status", "--porcelain=v1")
    if status_out.strip():
        return ToolResult(
            False,
            f"The working tree in {display} has uncommitted changes, so switching "
            f"branches could clobber them. Commit them first (git_commit), then switch.",
        )
    code, out = await _git(root, "switch", name)
    if code != 0:
        return ToolResult(False, f"Could not switch to '{name}': {out.strip()}")
    return ToolResult(
        True, f"Switched to '{name}' in {display}.",
        data={"repo": display, "branch": name, "created": False},
    )


async def _staged_summary(root: str) -> Tuple[str, List[Dict[str, str]]]:
    """The staged diff and the files in it, for the approval card and the result."""
    _, diff = await _git(root, "diff", "--cached", "--no-color")
    _, names = await _git(root, "diff", "--cached", "--name-status")
    files = []
    for line in names.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            # Renames report R<score>\told\tnew; the new path is the real one.
            files.append({"status": parts[0][:1], "path": parts[-1]})
    return diff, files


async def git_commit(
    sandbox: Sandbox,
    message: str,
    repo: Optional[str] = None,
    paths: Optional[List[str]] = None,
    approver: Optional[Any] = None,
) -> ToolResult:
    """Stage and commit, showing the human the exact staged diff first."""
    if not message or not str(message).strip():
        return ToolResult(False, "A commit needs a message that says why, not just that.")
    message = str(message).strip()

    root, problem = _locate_repo(sandbox, repo)
    if problem:
        return problem
    display = sandbox.relative(root)

    if paths:
        rel_paths = []
        for entry in paths:
            try:
                resolved = sandbox.resolve(str(entry))
            except SandboxError as exc:
                return ToolResult(False, f"Sandbox violation: {exc}")
            if not (resolved == root or resolved.startswith(root + os.sep)):
                return ToolResult(False, f"'{entry}' is not inside the repo {display}.")
            rel_paths.append(os.path.relpath(resolved, root))
        code, out = await _git(root, "add", "--", *rel_paths)
    else:
        code, out = await _git(root, "add", "-A")
    if code != 0:
        return ToolResult(False, f"git add failed in {display}: {out.strip()}")

    diff, files = await _staged_summary(root)
    if not files:
        return ToolResult(False, f"Nothing to commit in {display}: no changes are staged.")

    if approver is not None:
        declined = await approver.approve(
            f"git commit in {display}: {message}",
            diff,
            files,
        )
        if declined is not None:
            # Leave the tree exactly as it was found: staged nothing.
            await _git(root, "reset", "-q")
            return ToolResult(
                False,
                f"The commit was not made: {declined.summary}",
                data={"declined": True},
            )

    commit_args = []
    code, email = await _git(root, "config", "--get", "user.email")
    if code != 0 or not email.strip():
        name_id, email_id = FALLBACK_IDENTITY
        commit_args += ["-c", f"user.name={name_id}", "-c", f"user.email={email_id}"]
    commit_args += ["commit", "-m", message]

    code, out = await _git(root, *commit_args)
    if code != 0:
        return ToolResult(False, f"git commit failed in {display}: {out.strip()}")

    _, short = await _git(root, "rev-parse", "--short", "HEAD")
    _, branch_out = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    commit_hash = short.strip()
    branch = branch_out.strip()
    return ToolResult(
        True,
        f"Committed {commit_hash} on {branch} in {display}: {len(files)} file(s).",
        "\n".join(f"{f['status']}  {f['path']}" for f in files),
        # "committed", not "files": the agent loop treats data["files"] as
        # evidence the tool edited them and emits a patch event, which a commit
        # is not.
        {
            "repo": display, "commit": commit_hash, "branch": branch,
            "message": message, "committed": files,
        },
    )


# -- remote verbs -------------------------------------------------------------
#
# Push and pull are deliberately narrow: current branch, `origin`, and pull is
# ff-only. The panel is for sync; anything that rewrites history belongs to an
# agent run where a human can watch it think. Credentials come from
# skippy_github.git_env() — an askpass helper, never a token in a URL.

_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GIT_REMOTE_TIMEOUT = 180.0


async def _current_branch(root: str) -> Tuple[Optional[str], str]:
    code, out = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return None, out.strip()
    branch = out.strip()
    if branch == "HEAD":
        return None, "detached HEAD"
    return branch, ""


def _new_repo_home(sandbox: Sandbox) -> str:
    """Where a new or cloned repo lands: the first workspace root that is not
    itself a repo, else the first root — nesting a repo in a repo is legal,
    just not the first choice."""
    for root in sandbox.roots:
        if not os.path.isdir(os.path.join(root, ".git")):
            return root
    return sandbox.roots[0]


async def git_push(
    sandbox: Sandbox,
    repo: Optional[str] = None,
    approver: Optional[Any] = None,
) -> ToolResult:
    """Push the current branch to origin; `-u` when there is no upstream yet.

    With an approver (agent-initiated pushes), the human sees the outgoing
    commits first — a push is a write beyond the bench, so it asks the same
    way a commit does.
    """
    root, problem = _locate_repo(sandbox, repo)
    if problem:
        return problem
    display = sandbox.relative(root)

    branch, why = await _current_branch(root)
    if branch is None:
        return ToolResult(False, f"Cannot push from {display}: {why or 'no branch is checked out'}.")

    code, _ = await _git(root, "remote", "get-url", "origin")
    if code != 0:
        return ToolResult(
            False,
            f"{display} has no 'origin' remote, so there is nowhere to push. "
            f"Create a GitHub repo for it (git_new wires origin up), or add one by hand.",
        )

    code, _ = await _git(root, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    has_upstream = code == 0
    args = ["push", "origin", branch] if has_upstream else ["push", "-u", "origin", branch]

    if approver is not None:
        if has_upstream:
            _, preview = await _git(root, "log", "--oneline", "@{upstream}..HEAD")
        else:
            _, preview = await _git(root, "log", "--oneline", "-20")
            preview = f"(first push of this branch; most recent commits)\n{preview}"
        declined = await approver.approve(
            f"git push {branch} to origin in {display}",
            preview.strip(),
            [],
        )
        if declined is not None:
            return ToolResult(
                False,
                f"The push was not made: {declined.summary}",
                data={"declined": True},
            )

    code, out = await _git(
        root, *args, timeout=GIT_REMOTE_TIMEOUT, env_extra=skippy_github.git_env(),
    )
    if code != 0:
        text = out.strip()
        if "non-fast-forward" in text or "fetch first" in text or "[rejected]" in text:
            return ToolResult(
                False,
                f"Push rejected: origin has commits {display} does not. "
                f"Pull first (ff-only), then push again.",
                cap_text(text, MAX_TOOL_OUTPUT_CHARS),
            )
        if "Authentication failed" in text or "could not read Username" in text or "403" in text:
            return ToolResult(
                False,
                f"Push failed: GitHub did not accept the credentials. "
                f"Check the token in Settings and that it can write to this repo.",
                cap_text(text, MAX_TOOL_OUTPUT_CHARS),
            )
        return ToolResult(False, f"git push failed in {display}: {text.splitlines()[-1] if text else 'unknown error'}",
                          cap_text(text, MAX_TOOL_OUTPUT_CHARS))

    return ToolResult(
        True,
        f"Pushed {branch} to origin in {display}.",
        cap_text(out.strip(), MAX_TOOL_OUTPUT_CHARS),
        {"repo": display, "branch": branch, "pushed": True},
    )


async def git_pull(
    sandbox: Sandbox,
    repo: Optional[str] = None,
    approver: Optional[Any] = None,
) -> ToolResult:
    """Pull ff-only from origin. A diverged branch is an error naming the fix,
    never a surprise merge commit. With an approver (agent-initiated), the
    human confirms first — a pull rewrites the working tree."""
    root, problem = _locate_repo(sandbox, repo)
    if problem:
        return problem
    display = sandbox.relative(root)

    branch, why = await _current_branch(root)
    if branch is None:
        return ToolResult(False, f"Cannot pull into {display}: {why or 'no branch is checked out'}.")

    code, _ = await _git(root, "remote", "get-url", "origin")
    if code != 0:
        return ToolResult(False, f"{display} has no 'origin' remote, so there is nothing to pull from.")

    if approver is not None:
        declined = await approver.approve(
            f"git pull --ff-only origin {branch} into {display}",
            "Fast-forward only: local history is never rewritten, and a "
            "diverged branch fails cleanly instead of merging.",
            [],
        )
        if declined is not None:
            return ToolResult(
                False,
                f"The pull was not made: {declined.summary}",
                data={"declined": True},
            )

    code, out = await _git(
        root, "pull", "--ff-only", "origin", branch,
        timeout=GIT_REMOTE_TIMEOUT, env_extra=skippy_github.git_env(),
    )
    if code != 0:
        text = out.strip()
        if "Not possible to fast-forward" in text or "divergent" in text or "diverged" in text:
            return ToolResult(
                False,
                f"{display} and origin/{branch} have diverged, so a fast-forward pull is not possible. "
                f"Reconcile in an agent run or by hand (rebase or merge), then pull again.",
                cap_text(text, MAX_TOOL_OUTPUT_CHARS),
            )
        if "would be overwritten" in text:
            return ToolResult(
                False,
                f"Pull refused: uncommitted changes in {display} would be overwritten. "
                f"Commit them first, then pull.",
                cap_text(text, MAX_TOOL_OUTPUT_CHARS),
            )
        return ToolResult(False, f"git pull failed in {display}: {text.splitlines()[-1] if text else 'unknown error'}",
                          cap_text(text, MAX_TOOL_OUTPUT_CHARS))

    up_to_date = "up to date" in out.lower()
    summary = (
        f"{display} is already up to date with origin/{branch}."
        if up_to_date else f"Pulled origin/{branch} into {display} (fast-forward)."
    )
    return ToolResult(
        True, summary, cap_text(out.strip(), MAX_TOOL_OUTPUT_CHARS),
        {"repo": display, "branch": branch, "pulled": True, "up_to_date": up_to_date},
    )


async def git_new(
    sandbox: Sandbox,
    name: str,
    private: bool = True,
    github: bool = True,
) -> ToolResult:
    """A new repository under the workspace root: init, initial commit, and —
    when a token is set and `github` is true — the GitHub twin wired as origin
    and pushed. The local half never depends on the network half."""
    name = str(name or "").strip()
    if not _REPO_NAME_RE.match(name):
        return ToolResult(
            False,
            "Repo names are letters, digits, dots, dashes and underscores, "
            "starting with a letter or digit.",
        )

    home = _new_repo_home(sandbox)
    path = os.path.join(home, name)
    if os.path.exists(path):
        return ToolResult(False, f"'{sandbox.relative(path)}' already exists.")

    os.makedirs(path)
    code, out = await _git(path, "init", "-b", "main")
    if code != 0:
        return ToolResult(False, f"git init failed: {out.strip()}")

    readme = os.path.join(path, "README.md")
    with open(readme, "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\n")
    await _git(path, "add", "README.md")

    commit_args = []
    code, email = await _git(path, "config", "--get", "user.email")
    if code != 0 or not email.strip():
        name_id, email_id = FALLBACK_IDENTITY
        commit_args += ["-c", f"user.name={name_id}", "-c", f"user.email={email_id}"]
    code, out = await _git(path, *commit_args, "commit", "-m", "Initial commit")
    if code != 0:
        return ToolResult(False, f"Initial commit failed: {out.strip()}")

    display = sandbox.relative(path)
    data: Dict[str, Any] = {"repo": display, "path": path, "branch": "main", "github": False}

    if not github:
        return ToolResult(True, f"Created local repo {display} (no GitHub twin requested).", data=data)
    if not skippy_github.get_token():
        return ToolResult(
            True,
            f"Created local repo {display}. No GitHub token is set, so no remote was created — "
            f"paste a token in Settings and push later.",
            data=data,
        )

    try:
        remote = await skippy_github.create_repo(name, private=private)
    except skippy_github.GitHubError as exc:
        return ToolResult(
            True,
            f"Created local repo {display}, but GitHub said no: {exc}. "
            f"The local repo is fine; create the remote later and push.",
            data=data,
        )

    await _git(path, "remote", "add", "origin", remote["clone_url"])
    code, out = await _git(
        path, "push", "-u", "origin", "main",
        timeout=GIT_REMOTE_TIMEOUT, env_extra=skippy_github.git_env(),
    )
    if code != 0:
        return ToolResult(
            True,
            f"Created {display} and {remote['full_name']} on GitHub, but the first push failed: "
            f"{out.strip().splitlines()[-1] if out.strip() else 'unknown error'}. Push again from the panel.",
            data={**data, "github": True, "full_name": remote["full_name"], "html_url": remote["html_url"]},
        )

    data.update({"github": True, "full_name": remote["full_name"], "html_url": remote["html_url"]})
    return ToolResult(
        True,
        f"Created {display}, pushed to {remote['full_name']} "
        f"({'private' if remote['private'] else 'public'}).",
        data=data,
    )


async def git_clone(sandbox: Sandbox, full_name: str) -> ToolResult:
    """Clone one of the account's GitHub repos into the workspace root."""
    full_name = str(full_name or "").strip().removesuffix(".git")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$", full_name):
        return ToolResult(False, "Expected an owner/name pair, like 'blake/skippy'.")

    name = full_name.split("/", 1)[1]
    home = _new_repo_home(sandbox)
    path = os.path.join(home, name)
    if os.path.exists(path):
        return ToolResult(False, f"'{sandbox.relative(path)}' already exists; not cloning over it.")

    code, out = await _git(
        home, "clone", f"https://github.com/{full_name}.git", name,
        timeout=GIT_REMOTE_TIMEOUT, env_extra=skippy_github.git_env(),
    )
    if code != 0:
        text = out.strip()
        if "Authentication failed" in text or "could not read Username" in text or "not found" in text.lower():
            return ToolResult(
                False,
                f"Could not clone {full_name}: GitHub refused. If it is private, "
                f"check the token in Settings can read it.",
                cap_text(text, MAX_TOOL_OUTPUT_CHARS),
            )
        return ToolResult(False, f"git clone failed: {text.splitlines()[-1] if text else 'unknown error'}",
                          cap_text(text, MAX_TOOL_OUTPUT_CHARS))

    return ToolResult(
        True,
        f"Cloned {full_name} into {sandbox.relative(path)}.",
        data={"repo": sandbox.relative(path), "path": path, "full_name": full_name},
    )
