"""Shared fixtures.

Two rules this file enforces for the whole suite:

1. No test touches the real NAS, the real workspaces, or the developer's shell
   environment. Everything is redirected into tmp before any module is imported.
2. No test needs a model server, weights, or a network. `fake_llm` stands in for
   `mlx_lm.server`, and the CI job runs with the network blocked to prove it.
"""

import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
sys.path.insert(0, REPO_ROOT)

# Set before any skippy module is imported: these are read at import time.
os.environ.setdefault("SKIPPY_MEMORY_ROOT", "/tmp/skippy_test_memory")
os.environ.setdefault("SKIPPY_WORKSPACES_ROOT", "/tmp/skippy_test_workspaces")
os.environ.setdefault("SKIPPY_CHROMA_PATH", "/tmp/skippy_test_memory/chroma_db")

from tests.fake_llm import FakeLLM  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Clear SKIPPY_* overrides so a developer's shell cannot change results.

    The redirections above are re-applied, because dropping them would point a
    test at the real NAS.

    The memory root is per-test rather than a shared /tmp path. Since project memory
    began recording every run, a shared root meant each agent-loop test appended a
    session record to the same project and then read the accumulated pile back as
    opening context — so the prompt a test built depended on how many times the suite
    had been run before.
    """
    for key in list(os.environ):
        if key.startswith("SKIPPY_"):
            monkeypatch.delenv(key, raising=False)
    memory = tmp_path / "skippy_memory"
    monkeypatch.setenv("SKIPPY_MEMORY_ROOT", str(memory))
    monkeypatch.setenv("SKIPPY_WORKSPACES_ROOT", str(tmp_path / "skippy_workspaces"))
    monkeypatch.setenv("SKIPPY_CHROMA_PATH", str(memory / "chroma_db"))


@pytest.fixture(scope="session")
def fake_llm():
    server = FakeLLM(port=8771)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def routed_llm(fake_llm, monkeypatch):
    """Point every model role at the scripted server."""
    import skippy_llm

    for variable in ("SKIPPY_FAST_URL", "SKIPPY_HEAVY_URL", "SKIPPY_COMP_URL"):
        monkeypatch.setenv(variable, fake_llm.base_url)
    for variable in ("SKIPPY_FAST_MODEL", "SKIPPY_HEAVY_MODEL", "SKIPPY_COMP_MODEL"):
        monkeypatch.setenv(variable, "fake/test-model")
    skippy_llm.reload_registry()
    fake_llm.load([])
    yield fake_llm
    monkeypatch.undo()
    skippy_llm.reload_registry()


@pytest.fixture
def sample_repo(tmp_path):
    """A throwaway copy of tests/fixtures/sample_repo the agent may edit."""
    destination = tmp_path / "sample_repo"
    shutil.copytree(os.path.join(FIXTURES, "sample_repo"), destination)
    return str(destination)


@pytest.fixture
def sample_git_repo(sample_repo):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
            cwd=sample_repo,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return sample_repo
