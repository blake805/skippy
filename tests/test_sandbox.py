"""The path boundary. Every test here is about something the agent must NOT reach.

The threat model is a confused or manipulated model, not a determined attacker
with local shell access — the model can only propose paths, and web content or
third-party source it reads is untrusted input that might contain a suggestion
like "read ../../.ssh/id_ed25519".
"""

import os

import pytest

from skippy_sandbox import Sandbox, SandboxError, cap_text


@pytest.fixture
def roots(tmp_path):
    first = tmp_path / "repo_a"
    second = tmp_path / "repo_b"
    (first / "src").mkdir(parents=True)
    (first / "src" / "main.py").write_text("print('a')\n")
    second.mkdir()
    (second / "lib.py").write_text("print('b')\n")
    (tmp_path / "secret.txt").write_text("do not read me\n")
    return first, second, tmp_path


@pytest.fixture
def box(roots):
    first, second, _ = roots
    return Sandbox([str(first), str(second)])


# --- construction ---

def test_no_roots_is_refused():
    with pytest.raises(SandboxError):
        Sandbox([])


def test_a_nonexistent_root_is_refused(tmp_path):
    with pytest.raises(SandboxError) as exc:
        Sandbox([str(tmp_path / "nope")])
    assert "does not exist" in str(exc.value)


def test_a_file_cannot_be_a_root(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    with pytest.raises(SandboxError):
        Sandbox([str(target)])


def test_duplicate_roots_collapse(roots):
    first, _, _ = roots
    assert len(Sandbox([str(first), str(first)]).roots) == 1


def test_a_root_nested_in_another_root_is_dropped(roots):
    first, _, _ = roots
    box = Sandbox([str(first), str(first / "src")])
    assert box.roots == [os.path.realpath(str(first))]


def test_roots_are_symlink_resolved(tmp_path):
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "link_repo"
    link.symlink_to(real)

    box = Sandbox([str(link)])
    assert box.roots == [os.path.realpath(str(real))]


# --- what must be refused ---

def test_parent_traversal_is_refused(box, roots):
    with pytest.raises(SandboxError) as exc:
        box.resolve("../secret.txt")
    assert "outside the workspace roots" in str(exc.value)


def test_deep_parent_traversal_is_refused(box):
    with pytest.raises(SandboxError):
        box.resolve("src/../../../../../../etc/passwd")


def test_an_absolute_path_outside_the_roots_is_refused(box):
    with pytest.raises(SandboxError):
        box.resolve("/etc/passwd")


def test_a_home_relative_path_outside_the_roots_is_refused(box):
    with pytest.raises(SandboxError):
        box.resolve("~/.ssh/id_ed25519")


def test_a_symlink_inside_a_root_pointing_out_is_refused(box, roots):
    """The case a textual prefix check would miss, which is why resolution
    happens before validation rather than after."""
    first, _, tmp_path = roots
    escape = first / "escape"
    escape.symlink_to(tmp_path / "secret.txt")

    with pytest.raises(SandboxError):
        box.resolve("escape")


def test_a_symlinked_directory_inside_a_root_pointing_out_is_refused(box, roots):
    first, _, tmp_path = roots
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "loot.txt").write_text("x")
    (first / "bridge").symlink_to(outside)

    with pytest.raises(SandboxError):
        box.resolve("bridge/loot.txt")


def test_an_empty_or_blank_path_is_refused(box):
    for bad in ("", "   ", None):
        with pytest.raises(SandboxError):
            box.resolve(bad)


def test_a_nul_byte_is_refused_as_a_path_not_a_crash(box):
    # Without an explicit check this raises ValueError out of the os layer, which
    # a caller sees as a crash rather than a rejected path.
    with pytest.raises(SandboxError) as exc:
        box.resolve("src/main.py\x00.txt")
    assert "NUL" in str(exc.value)


def test_must_exist_reports_a_missing_file_separately(box):
    # Inside the sandbox but absent: a different error from "not allowed".
    with pytest.raises(SandboxError) as exc:
        box.resolve("src/ghost.py", must_exist=True)
    assert "does not exist" in str(exc.value)


def test_a_new_file_in_an_allowed_directory_resolves(box, roots):
    first, _, _ = roots
    resolved = box.resolve("src/brand_new.py")
    assert resolved == os.path.join(os.path.realpath(str(first)), "src", "brand_new.py")


def test_a_new_file_whose_parent_escapes_is_refused(box, roots):
    first, _, tmp_path = roots
    (first / "bridge").symlink_to(tmp_path)
    with pytest.raises(SandboxError):
        box.resolve("bridge/new_file.txt")


# --- what must be allowed ---

def test_a_relative_path_resolves_against_the_primary_root(box, roots):
    first, _, _ = roots
    assert box.resolve("src/main.py") == os.path.join(
        os.path.realpath(str(first)), "src", "main.py"
    )


def test_the_second_root_is_reachable_by_absolute_path(box, roots):
    _, second, _ = roots
    assert box.resolve(str(second / "lib.py")) == os.path.join(
        os.path.realpath(str(second)), "lib.py"
    )


def test_a_root_itself_resolves(box, roots):
    first, _, _ = roots
    assert box.resolve(str(first)) == os.path.realpath(str(first))


def test_a_sibling_directory_sharing_a_name_prefix_is_refused(tmp_path):
    """Root '/x/repo' must not admit '/x/repo_secrets' — the separator matters."""
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo_secrets").mkdir()
    (tmp_path / "repo_secrets" / "keys").write_text("x")

    box = Sandbox([str(tmp_path / "repo")])
    with pytest.raises(SandboxError):
        box.resolve(str(tmp_path / "repo_secrets" / "keys"))


# --- helpers ---

def test_contains_is_the_non_raising_form(box, roots):
    first, _, tmp_path = roots
    assert box.contains(str(first / "src" / "main.py")) is True
    assert box.contains(str(tmp_path / "secret.txt")) is False


def test_relative_prefixes_the_root_name_when_there_are_several(box, roots):
    first, second, _ = roots
    assert box.relative(box.resolve("src/main.py")) == os.path.join("repo_a", "src", "main.py")
    assert box.relative(box.resolve(str(second / "lib.py"))) == os.path.join("repo_b", "lib.py")


def test_relative_is_bare_with_a_single_root(roots):
    first, _, _ = roots
    box = Sandbox([str(first)])
    assert box.relative(box.resolve("src/main.py")) == os.path.join("src", "main.py")


def test_relative_leaves_a_foreign_path_alone(box):
    assert box.relative("/somewhere/else") == "/somewhere/else"


def test_repo_root_for_finds_the_enclosing_git_dir(box, roots):
    first, _, _ = roots
    (first / ".git").mkdir()
    assert box.repo_root_for(box.resolve("src/main.py")) == os.path.realpath(str(first))


def test_repo_root_for_returns_none_outside_a_repo(box):
    assert box.repo_root_for(box.resolve("src/main.py")) is None


def test_repo_root_for_never_walks_past_a_root(box, roots):
    """A .git above the root must not be claimed as the repo."""
    _, _, tmp_path = roots
    (tmp_path / ".git").mkdir()
    assert box.repo_root_for(box.resolve("src/main.py")) is None


# --- cap_text ---

def test_cap_text_keeps_both_ends():
    text = "A" * 100 + "B" * 100
    capped = cap_text(text, 100)
    assert capped.startswith("A" * 50)
    assert capped.endswith("B" * 50)
    assert "omitted 100 chars" in capped


def test_cap_text_leaves_short_text_untouched():
    assert cap_text("short", 100) == "short"


def test_cap_text_handles_empty():
    assert cap_text("", 10) == ""
    assert cap_text(None, 10) == ""
