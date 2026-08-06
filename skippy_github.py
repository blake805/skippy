"""The hub's GitHub connection: one token, held server-side, used two ways.

The repos live on this machine, so this machine is what talks to GitHub —
the app only ever sends the token once (Settings -> `github set_token`) and
from then on asks the hub to act. The token is used for two different things
and the split matters:

**The REST API** (httpx, `api.github.com`) answers who the token belongs to,
creates repositories, and lists the account's repositories for the clone
picker. No git involved.

**Git itself** (push, pull, clone over https) authenticates through a
`GIT_ASKPASS` helper rather than a token-bearing remote URL. A URL with the
token in it leaks into `git remote -v`, process listings, and error messages;
an askpass script that reads the token file at call time leaks nowhere and
survives token rotation without touching any repo's config.

Storage is a file, not an environment variable: `~/.skippy/github_token`,
mode 0600, next to the askpass helper it feeds. `SKIPPY_CONFIG_DIR` overrides
the directory so tests never touch a real home.
"""

from __future__ import annotations

import logging
import os
import stat
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("skippy_github")

API_BASE = "https://api.github.com"
API_TIMEOUT = 15.0

_ASKPASS_SCRIPT = """#!/bin/sh
# Answers git's credential prompts for github.com. Written by skippy_github;
# reads the token at call time so a rotated token needs no re-write here.
case "$1" in
  Username*) echo "x-access-token" ;;
  *) cat "{token_path}" ;;
esac
"""


class GitHubError(Exception):
    """A GitHub call failed in a way the caller should relay, not swallow."""


def _config_dir() -> str:
    return os.environ.get("SKIPPY_CONFIG_DIR") or os.path.expanduser("~/.skippy")


def _token_path() -> str:
    return os.path.join(_config_dir(), "github_token")


def _askpass_path() -> str:
    return os.path.join(_config_dir(), "github_askpass.sh")


def get_token() -> str:
    try:
        with open(_token_path(), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def set_token(token: str) -> None:
    """Store (or, with an empty token, forget) the GitHub token."""
    token = (token or "").strip()
    directory = _config_dir()
    if not token:
        for path in (_token_path(), _askpass_path()):
            try:
                os.remove(path)
            except OSError:
                pass
        return

    os.makedirs(directory, mode=0o700, exist_ok=True)
    token_path = _token_path()
    # 0600 before content: the file must never be readable in between.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")

    askpass = _askpass_path()
    fd = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_ASKPASS_SCRIPT.format(token_path=token_path))
    os.chmod(askpass, stat.S_IRWXU)


def git_env() -> Dict[str, str]:
    """Environment additions that let git reach GitHub with the stored token.

    Harmless without a token: prompts are disabled either way, so an
    unauthenticated push fails fast with git's own message instead of
    hanging on a prompt nobody will ever see.
    """
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if get_token() and os.path.exists(_askpass_path()):
        env["GIT_ASKPASS"] = _askpass_path()
    return env


# -- the REST side ------------------------------------------------------------


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _request(method: str, path: str, token: str, json: Optional[dict] = None) -> Any:
    if not token:
        raise GitHubError("No GitHub token is set. Paste one in SkippyMac's Settings.")
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        try:
            response = await client.request(
                method, API_BASE + path, headers=_headers(token), json=json,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"Could not reach GitHub: {exc}") from exc
    if response.status_code == 401:
        raise GitHubError("GitHub rejected the token (401). It may be expired or revoked.")
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("message", "")
        except Exception:
            pass
        raise GitHubError(f"GitHub said {response.status_code}: {detail or response.text[:200]}")
    return response.json()


async def whoami(token: Optional[str] = None) -> Dict[str, Any]:
    """Who the token belongs to; the proof a pasted token actually works."""
    data = await _request("GET", "/user", token if token is not None else get_token())
    return {"login": data.get("login", ""), "name": data.get("name") or ""}


async def create_repo(name: str, private: bool = True, description: str = "") -> Dict[str, Any]:
    """A new repository on the account the token belongs to."""
    data = await _request(
        "POST", "/user/repos", get_token(),
        json={
            "name": name,
            "private": bool(private),
            "description": description,
            # The local side brings the initial commit; an auto-initialised
            # remote would make the very first push a merge.
            "auto_init": False,
        },
    )
    return {
        "full_name": data.get("full_name", ""),
        "clone_url": data.get("clone_url", ""),
        "html_url": data.get("html_url", ""),
        "private": bool(data.get("private", private)),
    }


async def list_repos() -> List[Dict[str, Any]]:
    """The account's repositories, newest activity first, for the clone picker."""
    data = await _request(
        "GET", "/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator",
        get_token(),
    )
    return [
        {
            "full_name": item.get("full_name", ""),
            "name": item.get("name", ""),
            "private": bool(item.get("private")),
            "description": item.get("description") or "",
            "updated": item.get("updated_at") or "",
        }
        for item in data
        if isinstance(item, dict)
    ]
