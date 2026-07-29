import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
sys.path.insert(0, REPO_ROOT)

# Keep every test off the real NAS paths.
os.environ.setdefault("SKIPPY_MEMORY_ROOT", "/tmp/skippy_test_memory")
os.environ.setdefault("SKIPPY_WORKSPACES_ROOT", "/tmp/skippy_test_workspaces")

from tests.fake_llm import FakeLLM  # noqa: E402


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


@pytest.fixture
def sandbox(sample_repo):
    from skippy_agent_tools import Sandbox

    return Sandbox([sample_repo])


@pytest.fixture
def tool_ctx(sandbox):
    from skippy_agent_tools import ToolContext

    return ToolContext(sandbox=sandbox)
