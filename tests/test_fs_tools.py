"""The read-only filesystem tools.

Two themes: results have to be useful to a model with a limited context (bounded,
line-numbered, relative paths), and nothing produced by walking or globbing may
escape the sandbox even when the starting point was legitimate.
"""

import os

import pytest

import skippy_fs
from skippy_sandbox import Sandbox, SandboxError


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("import os\n\n\ndef run():\n    return 42\n")
    (root / "src" / "util.py").write_text("def helper():\n    pass\n")
    (root / "README.md").write_text("# repo\n")
    # A dotted directory the agent must be able to see: lineage B hid all of these.
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
    (root / ".gitignore").write_text("venv/\n")
    # Noise that must be pruned.
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00\x01binary")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (tmp_path / "outside.txt").write_text("secret\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


@pytest.fixture
def second_repo(tmp_path):
    """A second, unrelated workspace root. Working across repos is a requirement,
    not a bonus, so the default behaviour of every search tool has to span them."""
    root = tmp_path / "other"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "convert.py").write_text("def mm_to_inch(mm):\n    return mm / 25.4\n")
    # Same basename as a file in the first repo, to prove results stay distinguishable.
    (root / "README.md").write_text("# other\n")
    return root


@pytest.fixture
def multi_box(repo, second_repo):
    return Sandbox([str(repo), str(second_repo)])


# --- list_dir ---

def test_list_dir_shows_the_tree(box):
    result = skippy_fs.list_dir(box, ".")
    assert result.ok
    assert "src/" in result.content
    assert "README.md" in result.content


def test_list_dir_shows_dotted_directories(box):
    """.github must be visible: an agent working on this repo needs its CI config."""
    result = skippy_fs.list_dir(box, ".", depth=3)
    assert ".github/" in result.content
    assert "ci.yml" in result.content
    assert ".gitignore" in result.content


def test_list_dir_prunes_noise(box):
    result = skippy_fs.list_dir(box, ".", depth=4)
    assert "__pycache__" not in result.content
    assert "node_modules" not in result.content


def test_list_dir_respects_depth(box):
    shallow = skippy_fs.list_dir(box, ".", depth=1)
    assert "src/" in shallow.content
    assert "main.py" not in shallow.content


def test_list_dir_clamps_absurd_depth(box):
    assert skippy_fs.list_dir(box, ".", depth=9999).ok
    assert skippy_fs.list_dir(box, ".", depth=0).ok


def test_list_dir_on_a_file_says_use_read_file(box):
    result = skippy_fs.list_dir(box, "README.md")
    assert not result.ok
    assert "read_file" in result.summary


def test_list_dir_refuses_to_leave_the_sandbox(box):
    with pytest.raises(SandboxError):
        skippy_fs.list_dir(box, "../")


def test_list_dir_shows_but_does_not_follow_an_escaping_symlink(box, repo, tmp_path):
    (repo / "escape").symlink_to(tmp_path / "outside.txt")
    result = skippy_fs.list_dir(box, ".")
    assert "escape ->" in result.content
    assert "[outside workspace]" in result.content
    assert "secret" not in result.content


def test_list_dir_truncates_a_huge_directory(box, repo):
    big = repo / "many"
    big.mkdir()
    for index in range(600):
        (big / f"f{index}.txt").write_text("x")

    result = skippy_fs.list_dir(box, "many", depth=1)
    assert result.data["truncated"] is True
    assert "truncated" in result.content


# --- read_file ---

def test_read_file_numbers_the_lines(box):
    result = skippy_fs.read_file(box, "src/main.py")
    assert result.ok
    assert "     1| import os" in result.content
    assert result.data["total_lines"] == 5


def test_read_file_honours_a_line_range(box):
    result = skippy_fs.read_file(box, "src/main.py", start_line=4, end_line=5)
    assert "     4| def run():" in result.content
    assert "import os" not in result.content


def test_read_file_reports_a_relative_path_not_an_absolute_one(box):
    result = skippy_fs.read_file(box, "src/main.py")
    assert result.data["path"] == os.path.join("src", "main.py")


def test_read_file_rejects_a_start_past_the_end(box):
    result = skippy_fs.read_file(box, "src/main.py", start_line=500)
    assert not result.ok
    assert "past end of file" in result.summary


def test_read_file_rejects_an_inverted_range(box):
    result = skippy_fs.read_file(box, "src/main.py", start_line=4, end_line=2)
    assert not result.ok
    assert "before start_line" in result.summary


def test_read_file_on_a_directory_says_use_list_dir(box):
    result = skippy_fs.read_file(box, "src")
    assert not result.ok
    assert "list_dir" in result.summary


def test_read_file_refuses_a_binary_rather_than_returning_mojibake(box, repo):
    (repo / "blob.bin").write_bytes(b"\x7fELF\x00\x00\x00\x00" + bytes(range(256)))
    result = skippy_fs.read_file(box, "blob.bin")
    assert not result.ok
    assert result.data["binary"] is True
    assert "disassembler" in result.summary


def test_read_file_refuses_an_enormous_file(box, repo, monkeypatch):
    monkeypatch.setattr(skippy_fs, "MAX_READ_BYTES", 1024)
    (repo / "big.txt").write_text("x" * 4096)

    result = skippy_fs.read_file(box, "big.txt")
    assert not result.ok
    assert "read limit" in result.summary
    assert "grep" in result.summary


def test_read_file_refuses_to_leave_the_sandbox(box):
    with pytest.raises(SandboxError):
        skippy_fs.read_file(box, "../outside.txt")


def test_read_file_handles_an_empty_file(box, repo):
    (repo / "empty.py").write_text("")
    result = skippy_fs.read_file(box, "empty.py")
    assert result.ok
    assert result.data["total_lines"] == 0


# --- grep ---

async def test_grep_finds_a_match_with_a_relative_path(box):
    result = await skippy_fs.grep(box, "def run")
    assert result.ok
    assert result.data["matches"] == 1
    assert os.path.join("src", "main.py") in result.content
    assert str(box.primary) not in result.content


async def test_grep_reports_no_matches_as_success(box):
    result = await skippy_fs.grep(box, "notpresentanywhere")
    assert result.ok
    assert result.data["matches"] == 0


async def test_grep_rejects_a_bad_regex_as_a_tool_error(box):
    # Must be correctable by the model, not an exception out of the loop.
    result = await skippy_fs.grep(box, "def (unclosed")
    assert not result.ok
    assert "Invalid regular expression" in result.summary


async def test_grep_honours_ignore_case(box):
    assert (await skippy_fs.grep(box, "DEF RUN")).data["matches"] == 0
    assert (await skippy_fs.grep(box, "DEF RUN", ignore_case=True)).data["matches"] == 1


async def test_grep_honours_a_glob(box):
    result = await skippy_fs.grep(box, "def", glob="util.py")
    assert result.data["matches"] == 1
    assert "util.py" in result.content


async def test_grep_searches_dotted_directories(box):
    result = await skippy_fs.grep(box, "name: CI")
    assert result.data["matches"] == 1
    assert "ci.yml" in result.content


async def test_grep_prunes_noise_directories(box):
    result = await skippy_fs.grep(box, "left-pad|module.exports")
    assert result.data["matches"] == 0


async def test_grep_caps_results(box, repo):
    for index in range(30):
        (repo / f"hit{index}.txt").write_text("findme\n")
    result = await skippy_fs.grep(box, "findme", max_results=5)
    assert len(result.content.splitlines()) == 5
    assert "showing first 5" in result.summary


async def test_grep_refuses_a_path_outside_the_sandbox(box):
    with pytest.raises(SandboxError):
        await skippy_fs.grep(box, "secret", path="../")


async def test_the_python_fallback_agrees_with_ripgrep(box, monkeypatch):
    """The fallback runs on machines without rg, so it must not behave differently."""
    if not skippy_fs._rg_available():
        pytest.skip("ripgrep not installed, nothing to compare against")

    with_rg = await skippy_fs.grep(box, "def")
    monkeypatch.setattr(skippy_fs, "_rg_available", lambda: False)
    without_rg = await skippy_fs.grep(box, "def")

    assert without_rg.data["matches"] == with_rg.data["matches"]
    assert set(without_rg.content.splitlines()) == set(with_rg.content.splitlines())


async def test_the_python_fallback_also_prunes_and_skips_binaries(box, monkeypatch):
    monkeypatch.setattr(skippy_fs, "_rg_available", lambda: False)
    result = await skippy_fs.grep(box, "module.exports")
    assert result.data["matches"] == 0


# --- glob_files ---

def test_glob_files_matches_recursively(box):
    result = skippy_fs.glob_files(box, "**/*.py")
    assert result.ok
    assert os.path.join("src", "main.py") in result.content
    assert result.data["count"] == 2


def test_glob_files_prunes_noise(box):
    result = skippy_fs.glob_files(box, "**/*.js")
    assert result.data["count"] == 0


def test_glob_files_results_never_escape_the_sandbox(box, repo, tmp_path):
    """pathlib validates nothing, so a symlinked directory could smuggle a path out."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "loot.py").write_text("secret\n")
    (repo / "bridge").symlink_to(outside)

    result = skippy_fs.glob_files(box, "**/*.py")
    assert "loot.py" not in result.content


def test_glob_files_requires_a_pattern(box):
    assert not skippy_fs.glob_files(box, "").ok


# --- working across several repositories ---
#
# Each of these failed before the tools were changed to span roots: they searched
# only the primary root and reported "no matches" for files that were plainly there,
# which is the worst possible failure because it looks like a definitive answer.

@pytest.mark.asyncio
async def test_grep_without_a_path_searches_every_root(multi_box):
    result = await skippy_fs.grep(multi_box, "mm_to_inch")
    assert result.ok
    assert result.data["matches"] == 1
    assert "convert.py" in result.content


@pytest.mark.asyncio
async def test_grep_spans_roots_in_one_call(multi_box):
    result = await skippy_fs.grep(multi_box, "^# ", max_results=20)
    # One README per repo, and both have to appear.
    assert "other/README.md" in result.content
    assert "repo/README.md" in result.content


@pytest.mark.asyncio
async def test_the_python_fallback_also_spans_roots(multi_box, monkeypatch):
    """The fallback loops over roots itself, so it needs its own multi-root check."""
    monkeypatch.setattr(skippy_fs, "_rg_available", lambda: False)
    result = await skippy_fs.grep(multi_box, "^# ", max_results=20)
    assert "other/README.md" in result.content
    assert "repo/README.md" in result.content


@pytest.mark.asyncio
async def test_grep_with_an_explicit_path_stays_in_that_root(multi_box, second_repo):
    result = await skippy_fs.grep(multi_box, "^# ", path=str(second_repo))
    assert "other/README.md" in result.content
    assert "repo/README.md" not in result.content


def test_glob_without_a_path_searches_every_root(multi_box):
    result = skippy_fs.glob_files(multi_box, "**/*.py")
    assert "other/lib/convert.py" in result.content
    assert "repo/src/main.py" in result.content


def test_glob_qualifies_same_named_files_by_repo(multi_box):
    result = skippy_fs.glob_files(multi_box, "README.md")
    assert sorted(result.content.splitlines()) == ["other/README.md", "repo/README.md"]


def test_list_dir_without_a_path_shows_every_root(multi_box):
    result = skippy_fs.list_dir(multi_box)
    assert result.ok
    assert result.data["roots"] == 2
    assert "repo/" in result.content
    assert "other/" in result.content


def test_list_dir_treats_dot_as_the_whole_workspace_when_multi_root(multi_box):
    # There is no single current directory across several repos, so "." cannot mean
    # just the first one.
    assert skippy_fs.list_dir(multi_box, ".").content == skippy_fs.list_dir(multi_box).content


def test_list_dir_with_one_root_still_lists_that_root_plainly(box):
    # A single root needs no qualification, so it stays "./" and the multi-root
    # section header never appears.
    result = skippy_fs.list_dir(box)
    assert result.ok
    assert "roots" not in result.data
    assert result.content.startswith("./")


# --- build_sandbox ---

def test_build_sandbox_reads_the_environment(repo, monkeypatch):
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(repo))
    box = skippy_fs.build_sandbox()
    assert box.roots == [os.path.realpath(str(repo))]


def test_build_sandbox_accepts_several_roots(repo, tmp_path, monkeypatch):
    second = tmp_path / "other"
    second.mkdir()
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", os.pathsep.join([str(repo), str(second)]))
    assert len(skippy_fs.build_sandbox().roots) == 2


def test_build_sandbox_with_nothing_configured_fails_loudly(monkeypatch):
    # An agent with no roots reaches nothing, which is the right failure mode.
    monkeypatch.delenv("SKIPPY_WORKSPACE_ROOTS", raising=False)
    with pytest.raises(SandboxError) as exc:
        skippy_fs.build_sandbox()
    assert "SKIPPY_WORKSPACE_ROOTS" in str(exc.value)


# --- ToolResult rendering ---

def test_observation_marks_failure_so_the_model_can_tell(box):
    failed = skippy_fs.read_file(box, "src")
    assert failed.as_observation().startswith("ERROR: ")
    assert skippy_fs.read_file(box, "README.md").as_observation().startswith("OK: ")


def test_event_content_is_capped(box, repo):
    (repo / "long.txt").write_text("line\n" * 20000)
    event = skippy_fs.read_file(box, "long.txt").as_event()
    assert len(event["content"]) <= 24_000 + 200
