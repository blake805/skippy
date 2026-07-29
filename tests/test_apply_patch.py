"""The write path.

Two things are being pinned down. Atomicity: a patch either lands completely or
changes nothing, including when the failure is on the last file of a batch. And
text safety: this is the only module that can destroy data, so the cases where
lineage B silently corrupted files each have a test named after the corruption.
"""

import json
import os
import stat

import pytest

import skippy_edit
from skippy_edit import apply_patch
from skippy_sandbox import Sandbox


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    (root / "calc" / "__init__.py").write_text("from .ops import add, mul\n")
    (root / "README.md").write_text("# calc\n\nA calculator.\n")
    (tmp_path / "outside.txt").write_text("secret\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


def read(repo, rel):
    return (repo / rel).read_text()


# --- the core guarantee: all or nothing ---

def test_a_multi_file_patch_applies_every_edit(box, repo):
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "def add(a, b):", "replace": "def add(a, b, c=0):"},
        {"path": "calc/__init__.py", "search": "add, mul", "replace": "add, mul, sub"},
        {"path": "calc/new.py", "action": "create", "content": "def sub(a, b):\n    return a - b\n"},
    ])
    assert result.ok, result.content
    assert "def add(a, b, c=0):" in read(repo, "calc/ops.py")
    assert "add, mul, sub" in read(repo, "calc/__init__.py")
    assert (repo / "calc" / "new.py").exists()
    assert len(result.data["files"]) == 3


def test_one_bad_edit_writes_nothing(box, repo):
    before = read(repo, "calc/ops.py")
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "def add(a, b):", "replace": "def add(a, b, c=0):"},
        {"path": "calc/__init__.py", "search": "text that is not there", "replace": "x"},
    ])
    assert not result.ok
    # The good edit in the same batch must not have landed.
    assert read(repo, "calc/ops.py") == before


def test_a_failure_on_the_last_file_rolls_back_the_earlier_ones(box, repo, monkeypatch):
    """The interesting failure: writes have already succeeded when one breaks."""
    before = read(repo, "calc/ops.py")
    real_write = skippy_edit._atomic_write
    calls = {"n": 0}

    def explode_on_second(path, payload):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_write(path, payload)

    monkeypatch.setattr(skippy_edit, "_atomic_write", explode_on_second)
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"},
        {"path": "calc/__init__.py", "search": "add, mul", "replace": "add, mul, sub"},
    ])

    assert not result.ok
    assert "rolled back" in result.summary
    assert read(repo, "calc/ops.py") == before
    assert result.data["rolled_back"] == 1


def test_a_failed_rollback_reports_a_mixed_state_instead_of_claiming_success(
    box, repo, monkeypatch, tmp_path
):
    """The one failure the user has to act on, so it must never be understated."""
    journal = tmp_path / "journal"

    def always_explode(path, payload):
        raise OSError("filesystem went away")

    # Let the first write land, then break both the second write and the rollback.
    real_write = skippy_edit._atomic_write
    calls = {"n": 0}

    def explode_after_first(path, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(path, payload)
        raise OSError("filesystem went away")

    monkeypatch.setattr(skippy_edit, "_atomic_write", explode_after_first)
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"},
        {"path": "calc/__init__.py", "search": "add, mul", "replace": "add, mul, sub"},
    ], journal_dir=str(journal))

    assert not result.ok
    assert result.data["mixed_state"] is True
    assert "mixed state" in result.summary
    # And it has to say where the pre-images are, or recovery is guesswork.
    assert str(journal) in result.summary
    assert result.data["unrestored"]


def test_sequential_edits_to_one_file_stack(box, repo):
    """Each edit validates against the previous result, not against the disk."""
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "def add(a, b):", "replace": "def plus(a, b):"},
        {"path": "calc/ops.py", "search": "def plus(a, b):", "replace": "def plus(a, b, c=0):"},
    ])
    assert result.ok, result.content
    assert "def plus(a, b, c=0):" in read(repo, "calc/ops.py")


def test_every_problem_is_reported_at_once(box):
    """One round trip to fix a batch, rather than one per problem."""
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "nope", "replace": "x"},
        {"path": "calc/ops.py", "action": "bogus"},
        {"path": "missing.py", "search": "a", "replace": "b"},
    ])
    assert not result.ok
    assert len(result.data["problems"]) == 3


# --- text safety: the cases lineage B corrupted ---

def test_a_non_utf8_file_is_refused_rather_than_mangled(box, repo):
    """Lineage B read with errors='replace' and wrote the result back, so a latin-1
    byte in an old C header became U+FFFD and the original byte was gone."""
    target = repo / "legacy.c"
    original = b"/* copyright \xa9 2019 */\nint main(void){return 0;}\n"
    target.write_bytes(original)

    result = apply_patch(box, [
        {"path": "legacy.c", "search": "return 0", "replace": "return 1"},
    ])
    assert not result.ok
    assert "not valid UTF-8" in result.content
    assert target.read_bytes() == original


def test_a_binary_file_is_refused(box, repo):
    target = repo / "blob.bin"
    original = b"\x7fELF\x02\x00\x00\x00payload"
    target.write_bytes(original)
    result = apply_patch(box, [{"path": "blob.bin", "search": "payload", "replace": "x"}])
    assert not result.ok
    assert "binary" in result.content
    assert target.read_bytes() == original


def test_crlf_line_endings_survive_an_edit(box, repo):
    """A one-word edit in a CRLF file must not rewrite every line in it."""
    target = repo / "windows.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    result = apply_patch(box, [{"path": "windows.txt", "search": "beta", "replace": "BETA"}])
    assert result.ok, result.content
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_a_crlf_search_string_still_matches_a_crlf_file(box, repo):
    """The model echoes back text it read, so both sides get normalized."""
    target = repo / "windows.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = apply_patch(box, [
        {"path": "windows.txt", "search": "one\r\ntwo", "replace": "one\r\nTWO"},
    ])
    assert result.ok, result.content
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_lf_files_stay_lf(box, repo):
    result = apply_patch(box, [{"path": "calc/ops.py", "search": "a + b", "replace": "b + a"}])
    assert result.ok
    assert b"\r\n" not in (repo / "calc" / "ops.py").read_bytes()


def test_a_stray_crlf_does_not_flip_an_lf_file(box, repo):
    """Dominant style wins, so one pasted line does not convert the whole file."""
    target = repo / "mostly_lf.txt"
    target.write_bytes(b"a\nb\nc\r\nd\ne\n")
    result = apply_patch(box, [{"path": "mostly_lf.txt", "search": "a\n", "replace": "A\n"}])
    assert result.ok
    assert target.read_bytes().count(b"\r\n") == 0


def test_a_file_too_large_to_edit_is_refused(box, repo, monkeypatch):
    monkeypatch.setattr(skippy_edit, "MAX_EDIT_BYTES", 100)
    target = repo / "big.txt"
    target.write_text("x" * 500)
    result = apply_patch(box, [{"path": "big.txt", "search": "x", "replace": "y"}])
    assert not result.ok
    assert "edit limit" in result.content


# --- occurrence targeting ---

def test_an_ambiguous_search_is_rejected_with_guidance(box, repo):
    (repo / "dup.py").write_text("value = 1\nvalue = 1\n")
    result = apply_patch(box, [{"path": "dup.py", "search": "value = 1", "replace": "value = 2"}])
    assert not result.ok
    assert "matched 2 times" in result.content
    # The error has to name the ways out, or the model just retries the same thing.
    assert "replace_all" in result.content and "occurrence" in result.content


def test_replace_all_changes_every_occurrence(box, repo):
    (repo / "dup.py").write_text("value = 1\nvalue = 1\n")
    result = apply_patch(
        box, [{"path": "dup.py", "search": "value = 1", "replace": "value = 2", "replace_all": True}]
    )
    assert result.ok
    assert read(repo, "dup.py") == "value = 2\nvalue = 2\n"


def test_occurrence_targets_one_match(box, repo):
    (repo / "dup.py").write_text("v = 1\nv = 1\nv = 1\n")
    result = apply_patch(
        box, [{"path": "dup.py", "search": "v = 1", "replace": "v = 9", "occurrence": 2}]
    )
    assert result.ok
    assert read(repo, "dup.py") == "v = 1\nv = 9\nv = 1\n"


def test_occurrence_counts_non_overlapping_matches(box, repo):
    """Lineage B advanced one character at a time, so for 'aa' in 'aaaa' it accepted
    occurrence=2 — which str.count agrees exists — and then edited the overlapping
    span at index 1, silently changing text the model did not point at."""
    (repo / "over.txt").write_text("aaaa")
    result = apply_patch(
        box, [{"path": "over.txt", "search": "aa", "replace": "X", "occurrence": 2}]
    )
    assert result.ok, result.content
    assert read(repo, "over.txt") == "aaX"


def test_occurrence_out_of_range_is_rejected(box, repo):
    (repo / "dup.py").write_text("v = 1\nv = 1\n")
    result = apply_patch(
        box, [{"path": "dup.py", "search": "v = 1", "replace": "v = 9", "occurrence": 5}]
    )
    assert not result.ok
    assert "out of range" in result.content


def test_replace_all_and_occurrence_together_is_rejected(box, repo):
    (repo / "dup.py").write_text("v = 1\nv = 1\n")
    result = apply_patch(box, [{
        "path": "dup.py", "search": "v = 1", "replace": "v = 9",
        "replace_all": True, "occurrence": 1,
    }])
    assert not result.ok
    assert "not both" in result.content


def test_a_missing_search_string_says_to_re_read(box):
    result = apply_patch(box, [{"path": "calc/ops.py", "search": "def divide", "replace": "x"}])
    assert not result.ok
    assert "byte-for-byte" in result.content and "re-read" in result.content


# --- create and delete ---

def test_create_refuses_to_clobber_without_overwrite(box, repo):
    result = apply_patch(box, [
        {"path": "README.md", "action": "create", "content": "# replaced\n"},
    ])
    assert not result.ok
    assert "already exists" in result.content
    assert read(repo, "README.md").startswith("# calc")


def test_create_with_overwrite_replaces_the_file(box, repo):
    result = apply_patch(box, [
        {"path": "README.md", "action": "create", "content": "# replaced\n", "overwrite": True},
    ])
    assert result.ok
    assert read(repo, "README.md") == "# replaced\n"
    assert result.data["files"][0]["action"] == "overwrite"


def test_create_makes_intermediate_directories(box, repo):
    result = apply_patch(box, [
        {"path": "a/b/c/deep.py", "action": "create", "content": "x = 1\n"},
    ])
    assert result.ok, result.content
    assert read(repo, "a/b/c/deep.py") == "x = 1\n"


def test_delete_removes_the_file(box, repo):
    result = apply_patch(box, [{"path": "README.md", "action": "delete"}])
    assert result.ok
    assert not (repo / "README.md").exists()


def test_delete_requires_an_existing_file(box):
    result = apply_patch(box, [{"path": "ghost.py", "action": "delete"}])
    assert not result.ok
    assert "does not exist" in result.content


def test_a_directory_cannot_be_edited(box):
    result = apply_patch(box, [{"path": "calc", "search": "a", "replace": "b"}])
    assert not result.ok
    assert "directory" in result.content


def test_an_unknown_action_lists_the_valid_ones(box):
    result = apply_patch(box, [{"path": "README.md", "action": "rename"}])
    assert not result.ok
    assert "edit" in result.content and "create" in result.content and "delete" in result.content


# --- the sandbox still applies ---

@pytest.mark.parametrize("escape", ["../outside.txt", "/etc/hosts", "~/.zshrc"])
def test_paths_outside_the_roots_are_refused(box, escape):
    result = apply_patch(box, [{"path": escape, "search": "a", "replace": "b"}])
    assert not result.ok
    assert "outside" in result.content or "resolves outside" in result.content


def test_a_symlinked_escape_is_refused(box, repo, tmp_path):
    os.symlink(tmp_path / "outside.txt", repo / "link.txt")
    result = apply_patch(box, [{"path": "link.txt", "search": "secret", "replace": "leaked"}])
    assert not result.ok
    assert (tmp_path / "outside.txt").read_text() == "secret\n"


def test_a_create_cannot_escape_the_sandbox(box, tmp_path):
    result = apply_patch(box, [
        {"path": "../planted.py", "action": "create", "content": "import os\n"},
    ])
    assert not result.ok
    assert not (tmp_path / "planted.py").exists()


# --- diffs, dry runs, no-ops ---

def test_dry_run_reports_a_diff_without_touching_the_disk(box, repo):
    before = read(repo, "calc/ops.py")
    result = apply_patch(
        box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}], dry_run=True
    )
    assert result.ok
    assert result.data["dry_run"] is True
    assert "-    return a + b" in result.content
    assert "+    return a + b + 0" in result.content
    assert read(repo, "calc/ops.py") == before


def test_the_diff_uses_relative_paths_and_counts_lines(box):
    result = apply_patch(
        box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}], dry_run=True
    )
    assert "a/calc/ops.py" in result.content
    assert "/Users" not in result.content and "/tmp" not in result.content
    assert result.data["files"][0] == {
        "path": os.path.join("calc", "ops.py"), "action": "edit", "added": 1, "removed": 1,
    }


def test_a_created_file_diffs_against_dev_null(box):
    result = apply_patch(
        box, [{"path": "fresh.py", "action": "create", "content": "x = 1\n"}], dry_run=True
    )
    assert "/dev/null" in result.content
    assert "+x = 1" in result.content


def test_a_file_without_a_trailing_newline_produces_a_readable_diff(box, repo):
    (repo / "nonl.txt").write_text("alpha\nbeta")
    result = apply_patch(
        box, [{"path": "nonl.txt", "search": "beta", "replace": "gamma"}], dry_run=True
    )
    assert result.ok
    # Every line of the diff has to end in a newline, or the next hunk header
    # splices onto the previous line and the diff is unreadable.
    assert all(line for line in result.content.splitlines())
    assert "+gamma" in result.content


def test_a_noop_patch_is_reported_as_such(box):
    result = apply_patch(box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b"}])
    assert result.ok
    assert "no-op" in result.summary
    assert result.data["files"] == []


def test_an_empty_edit_list_is_rejected(box):
    assert not apply_patch(box, []).ok
    assert not apply_patch(box, None).ok


def test_a_non_object_edit_is_rejected(box):
    result = apply_patch(box, ["not an object"])
    assert not result.ok
    assert "expected an object" in result.content


def test_the_diff_is_capped(box, repo, monkeypatch):
    monkeypatch.setattr(skippy_edit, "MAX_DIFF_CHARS", 500)
    (repo / "long.txt").write_text("line\n" * 2000)
    result = apply_patch(
        box, [{"path": "long.txt", "search": "line", "replace": "LINE", "replace_all": True}],
        dry_run=True,
    )
    assert result.ok
    assert len(result.content) < 1200


# --- writes are atomic and preserve metadata ---

def test_the_file_mode_is_preserved(box, repo):
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    result = apply_patch(box, [{"path": "run.sh", "search": "echo hi", "replace": "echo bye"}])
    assert result.ok
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o755


def test_no_temp_files_are_left_behind(box, repo):
    apply_patch(box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}])
    assert not [name for name in os.listdir(repo / "calc") if name.startswith(".skippy-")]


def test_a_failed_write_leaves_no_temp_files(box, repo, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", explode)
    apply_patch(box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}])
    assert not [name for name in os.listdir(repo / "calc") if name.startswith(".skippy-")]


# --- the journal ---

def test_the_journal_captures_pre_images_and_how_to_restore_them(box, repo, tmp_path):
    journal = tmp_path / "journal"
    before = read(repo, "calc/ops.py")
    result = apply_patch(
        box,
        [
            {"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"},
            {"path": "fresh.py", "action": "create", "content": "x = 1\n"},
        ],
        journal_dir=str(journal),
    )
    assert result.ok
    saved = result.data["journal"]
    manifest = json.loads(open(os.path.join(saved, "manifest.json"), encoding="utf-8").read())

    # Absolute paths and a restore instruction, or the pre-images are orphans.
    entries = {os.path.basename(e["path"]): e for e in manifest["files"]}
    assert os.path.isabs(entries["ops.py"]["path"])
    assert "restore" in manifest
    assert open(os.path.join(saved, entries["ops.py"]["pre_image"]), encoding="utf-8").read() == before
    # A created file has no pre-image; recovery is to delete it.
    assert entries["fresh.py"]["pre_image"] is None


def test_a_journal_that_cannot_be_written_does_not_block_the_patch(box, repo, tmp_path):
    """In-memory rollback is the real guarantee, so the journal is best-effort."""
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory\n")
    result = apply_patch(
        box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}],
        journal_dir=str(blocked),
    )
    assert result.ok
    assert result.data["journal"] is None
    assert "a + b + 0" in read(repo, "calc/ops.py")


def test_no_journal_is_written_on_a_dry_run(box, tmp_path):
    journal = tmp_path / "journal"
    apply_patch(
        box, [{"path": "calc/ops.py", "search": "a + b", "replace": "x"}],
        dry_run=True, journal_dir=str(journal),
    )
    assert not journal.exists()


# --- rendering to the model ---

def test_a_rejected_patch_reads_as_an_error_to_the_model(box):
    observation = apply_patch(box, [{"path": "calc/ops.py", "search": "no", "replace": "x"}])
    assert observation.as_observation().startswith("ERROR: ")


def test_an_applied_patch_names_the_files_and_line_counts(box):
    result = apply_patch(box, [
        {"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"},
        {"path": "README.md", "search": "A calculator.", "replace": "A small calculator."},
    ])
    assert result.ok
    assert "ops.py (+1/-1)" in result.summary
    assert "README.md (+1/-1)" in result.summary
