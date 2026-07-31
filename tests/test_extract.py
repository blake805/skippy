"""Carving, and the container that contains it.

Extraction is the only operation in the RE lane that points a stack of format parsers at
a hostile blob and asks them to write files, so most of this file is about the boundary
rather than about carving.

The container flags *are* the containment, which makes them worth asserting one by one:
they are a dozen strings in a list, any of which can be dropped in a refactor with no
test failing and no symptom until the day it matters. CI has no VM, so these run without
a runtime present — which is also why they are the tests that must exist. The live
behaviour of unblob inside the container is verified by hand and recorded in ADR 0019.
"""

import asyncio
import json
import os

import pytest

import skippy_extract
import skippy_re
from skippy_extract import ExtractError


@pytest.fixture
def notes_dir(tmp_path):
    return str(tmp_path / "notes")


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"NOTREALLYFIRMWARE" + b"\x00" * 128)
    return str(path)


@pytest.fixture
def pack(notes_dir, target):
    return skippy_re.open_pack(notes_dir, target=target, title="carve")


@pytest.fixture
def argv(tmp_path):
    """The vector as built for podman, which is the runtime we prefer."""
    return skippy_extract.container_argv(
        "/opt/homebrew/bin/podman",
        input_dir=str(tmp_path / "in"),
        input_name="firmware.bin",
        output_dir=str(tmp_path / "out"),
        name="skippy-unblob-test",
    )


# --- the container flags are the containment ------------------------------

def test_extraction_has_no_network(argv):
    """An extractor that reaches the network is exfiltrating or fetching, and both
    should fail rather than be observed. This also removes the callback half of any
    code-execution bug in a parser."""
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "none"


def test_extraction_drops_every_capability(argv):
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1].upper() == "ALL"


def test_extraction_cannot_gain_privileges(argv):
    assert "no-new-privileges" in " ".join(argv)


def test_the_artifact_is_mounted_read_only(argv):
    """The RE lane's standing guarantee is that the target is not modified. Extraction
    is the one operation that could break it, so the input mount is where that is
    enforced rather than trusted."""
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
    input_mount = [m for m in mounts if "/data/input" in m]
    assert input_mount, "the artifact must be mounted"
    assert input_mount[0].endswith(":ro")


def test_the_root_filesystem_is_not_writable(argv):
    """So a parser that writes outside the extraction directory fails rather than
    leaving something behind inside the image for a later run to pick up."""
    assert "--read-only" in argv
    tmpfs = [argv[i + 1] for i, part in enumerate(argv) if part == "--tmpfs"]
    assert any(entry.startswith("/tmp") for entry in tmpfs)


def test_memory_is_capped_and_cannot_spill_into_swap(argv):
    """A parser that allocates on a length field it read out of the file is the ordinary
    case, not the exotic one. Capping memory without capping swap just moves it."""
    assert argv[argv.index("--memory") + 1] == skippy_extract.CONTAINER_MEMORY
    assert argv[argv.index("--memory-swap") + 1] == skippy_extract.CONTAINER_MEMORY


def test_process_count_is_capped(argv):
    assert "--pids-limit" in argv


def test_the_worker_count_matches_the_cpu_cap(argv):
    """unblob defaults to one worker per host core, which inside a two-CPU container is
    a dozen processes contending over two."""
    assert argv[argv.index("--process-num") + 1] == argv[argv.index("--cpus") + 1]


def test_the_container_is_removed_after_the_run(argv):
    assert "--rm" in argv


@pytest.mark.parametrize("forbidden", [
    "--privileged",
    "--cap-add",
    "--security-opt=seccomp=unconfined",
    "--pid=host",
    "--net=host",
    "--userns=host",
])
def test_nothing_that_would_undo_the_boundary_is_present(argv, forbidden):
    joined = " ".join(argv)
    assert forbidden not in joined


def test_the_docker_socket_is_never_mounted(argv):
    """Mounting it would hand the extractor the ability to start an unconfined
    container, which is the standard way a container boundary is escaped in practice."""
    joined = " ".join(argv)
    assert "docker.sock" not in joined
    assert "podman.sock" not in joined


def test_only_the_two_intended_paths_are_mounted(argv, tmp_path):
    """A bind mount is the whole boundary. Anything else appearing here — the home
    directory, the notes root, `/` — would be the boundary in name only."""
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
    assert len(mounts) == 2
    for mount in mounts:
        source = mount.split(":")[0]
        assert source.startswith(str(tmp_path))


# --- the image is pinned, because output becomes evidence ------------------

def test_the_image_is_pinned_by_digest(argv):
    """unblob's docs recommend `--pull always` — right for a person at a terminal, wrong
    for a tool whose output is cited in a finding. A finding that rests on an extraction
    should be reproducible, and `:latest` means the image that produced it is gone."""
    image = [part for part in argv if "unblob" in part and "/" in part]
    assert image, "the image must be in the vector"
    assert "@sha256:" in image[0]
    assert not image[0].endswith(":latest")


def test_the_image_is_not_re_pulled_on_every_run(argv):
    assert argv[argv.index("--pull") + 1] == "missing"


def test_no_plugin_path_is_ever_passed(argv):
    """unblob loads plugins only from an explicit --plugins-path, which is the property
    whose absence turns binwalk 2.x's path traversal into code execution. Passing one
    would hand that back."""
    assert "--plugins-path" not in argv
    assert "-P" not in argv


# --- runtime selection ----------------------------------------------------

def test_podman_is_preferred(monkeypatch):
    """Rootless and daemonless, so there is no root daemon on the host to be part of the
    attack surface."""
    assert skippy_extract.RUNTIMES[0] == "podman"
    monkeypatch.delenv(skippy_extract.RUNTIME_ENV, raising=False)
    monkeypatch.setattr(
        skippy_extract.shutil, "which",
        lambda name: f"/usr/local/bin/{name}" if name in ("podman", "docker") else None,
    )
    assert skippy_extract.runtime_path().endswith("podman")


def test_docker_is_accepted_when_podman_is_absent(monkeypatch):
    monkeypatch.delenv(skippy_extract.RUNTIME_ENV, raising=False)
    monkeypatch.setattr(
        skippy_extract.shutil, "which",
        lambda name: "/usr/local/bin/docker" if name == "docker" else None,
    )
    assert skippy_extract.runtime_path().endswith("docker")


def test_docker_is_told_the_uid_because_it_does_not_map_one(tmp_path):
    """Rootless Podman already maps our uid to the container's root, so keep-id says it
    there. Docker does not, and without this the extracted files come out owned by root
    and cannot be read afterwards."""
    docker = skippy_extract.container_argv(
        "/usr/local/bin/docker", input_dir=str(tmp_path), input_name="f.bin",
        output_dir=str(tmp_path / "out"), name="n",
    )
    assert "--user" in docker
    assert argv_value(docker, "--user") == f"{os.getuid()}:{os.getgid()}"

    podman = skippy_extract.container_argv(
        "/opt/homebrew/bin/podman", input_dir=str(tmp_path), input_name="f.bin",
        output_dir=str(tmp_path / "out"), name="n",
    )
    assert argv_value(podman, "--userns") == "keep-id"
    assert "--user" not in podman


def argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_with_no_runtime_the_refusal_says_what_to_install(monkeypatch):
    """RE mode has to keep working without extraction, and the refusal has to be one a
    person can act on — including why the host is not an option."""
    monkeypatch.delenv(skippy_extract.RUNTIME_ENV, raising=False)
    monkeypatch.setattr(skippy_extract.shutil, "which", lambda name: None)
    with pytest.raises(ExtractError) as caught:
        skippy_extract.runtime_path()
    message = str(caught.value)
    assert "podman" in message
    assert "ADR 0019" in message
    assert not skippy_extract.available()


def test_extraction_refuses_cleanly_with_no_runtime(pack, monkeypatch):
    monkeypatch.delenv(skippy_extract.RUNTIME_ENV, raising=False)
    monkeypatch.setattr(skippy_extract.shutil, "which", lambda name: None)
    result = asyncio.run(skippy_extract.extract_artifact(pack))
    assert not result.ok
    assert "container runtime" in result.summary


# --- output goes in the pack, and stays there -----------------------------

def test_extracted_files_land_in_the_pack_not_a_workspace_root(pack, notes_dir):
    """These bytes came out of a hostile blob by way of a stack of format parsers. A
    workspace root is somewhere Skippy writes code, which is the last place they belong."""
    root = skippy_extract.quarantine_root(pack)
    assert root.startswith(os.path.realpath(notes_dir)) or notes_dir in root
    assert "quarantine" in root
    assert os.path.isdir(root)


@pytest.mark.parametrize("escape", [
    "../../../../etc/passwd",
    "/etc/passwd",
    "..",
    "../../pack.json",
    "sub/../../../../../../tmp/x",
])
def test_a_path_outside_the_quarantine_is_refused(pack, escape):
    """The model does get to name a file here, unlike anywhere else in the module, since
    the point of carving is to read what comes out. So this is the check that makes that
    safe."""
    with pytest.raises(ExtractError):
        skippy_extract.resolve_in_quarantine(pack, escape)


def test_a_symlink_out_of_the_quarantine_is_refused(pack, tmp_path):
    """Extraction produces symlinks, and one pointing at /etc is something unblob
    reports rather than something it can always prevent. So the check follows links
    before deciding, instead of trusting the path as written."""
    root = skippy_extract.quarantine_root(pack)
    secret = tmp_path / "outside.txt"
    secret.write_text("not yours")
    link = os.path.join(root, "escape")
    os.symlink(str(secret), link)
    with pytest.raises(ExtractError):
        skippy_extract.resolve_in_quarantine(pack, "escape")


def test_a_real_extracted_file_resolves(pack):
    root = skippy_extract.quarantine_root(pack)
    os.makedirs(os.path.join(root, "0001-firmware-bin", "squashfs-root", "bin"))
    inner = os.path.join(root, "0001-firmware-bin", "squashfs-root", "bin", "httpd")
    with open(inner, "wb") as handle:
        handle.write(b"\x7fELF")
    resolved = skippy_extract.resolve_in_quarantine(
        pack, "0001-firmware-bin/squashfs-root/bin/httpd"
    )
    assert resolved == os.path.realpath(inner)


def test_a_path_that_does_not_exist_says_to_list_first(pack):
    with pytest.raises(ExtractError, match="list_extracted"):
        skippy_extract.resolve_in_quarantine(pack, "nope/missing.bin")


def test_each_extraction_gets_its_own_directory(pack):
    """So a second carve does not overwrite the first, and the numbering says what
    happened in what order."""
    root = skippy_extract.quarantine_root(pack)
    first = skippy_extract._next_extraction_dir(root, "firmware.bin")
    second = skippy_extract._next_extraction_dir(root, "firmware.bin")
    assert first != second
    assert os.path.basename(first).startswith("0001-")
    assert os.path.basename(second).startswith("0002-")


# --- reading unblob's report, without depending on its schema -------------

def test_the_formats_found_are_lifted_out_of_the_report(tmp_path):
    """"What is this made of" is usually the whole question, and the answer is a line
    rather than a file tree."""
    report = {"results": [
        {"chunks": [{"handler_name": "squashfs_v4_le"}, {"handler_name": "gzip"}]},
        {"chunks": [{"handler_name": "gzip"}]},
    ]}
    (tmp_path / skippy_extract.REPORT_NAME).write_text(json.dumps(report))
    summary = skippy_extract.read_report(str(tmp_path))
    formats = dict(summary["formats"])
    assert formats["gzip"] == 2
    assert formats["squashfs_v4_le"] == 1


def test_a_blocked_path_traversal_is_surfaced_not_swallowed(tmp_path):
    """An image that attempts to write outside the extraction directory has told us
    something about itself, and it is very likely a weakness in whatever built it. unblob
    stops it; that is not a reason for us to stay quiet about it."""
    report = {"results": [{"reports": [{
        "problem": "Potential path traversal through symlink",
        "path": "../../etc/shadow",
        "severity": "WARNING",
    }]}]}
    (tmp_path / skippy_extract.REPORT_NAME).write_text(json.dumps(report))
    summary = skippy_extract.read_report(str(tmp_path))
    assert summary["problems"]
    assert "etc/shadow" in summary["problems"][0]


@pytest.mark.parametrize("content", ["", "not json at all", "[1, 2, 3]", "{}"])
def test_an_unreadable_report_does_not_fail_an_extraction_that_worked(tmp_path, content):
    """The schema is not ours and the project is under heavy development, so the report
    is read opportunistically. Files on disk are the ground truth."""
    (tmp_path / skippy_extract.REPORT_NAME).write_text(content)
    summary = skippy_extract.read_report(str(tmp_path))
    assert summary["formats"] == []
    assert summary["problems"] == []


def test_a_missing_report_is_not_an_error(tmp_path):
    assert skippy_extract.read_report(str(tmp_path))["formats"] == []


def test_the_report_is_not_listed_as_something_that_came_out_of_the_image(tmp_path):
    """It is our instrumentation, and presenting it as extracted content would be a
    small lie that a later reader has to work out."""
    (tmp_path / skippy_extract.REPORT_NAME).write_text("{}")
    (tmp_path / "real.bin").write_bytes(b"\x00" * 10)
    lines, count, _total = skippy_extract.walk_quarantine(str(tmp_path))
    assert count == 1
    assert not any(skippy_extract.REPORT_NAME in line for line in lines)


# --- the bomb, which needs no vulnerability at all -----------------------

def test_the_output_cap_is_enforced_by_a_watchdog_because_no_flag_does_it(tmp_path):
    """A decompression bomb needs no bug — just a compression ratio — and no runtime flag
    bounds writes to a bind mount. So this is a poll-and-kill, and the thing worth
    asserting is that the measurement is right."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * 4096)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "more.bin").write_bytes(b"\x00" * 2048)
    assert skippy_extract._tree_size(str(tmp_path)) == 6144


def test_recursion_depth_is_bounded(pack, monkeypatch):
    """An archive that contains itself unpacks forever. unblob defaults to depth 10; the
    default here is lower because the cost of guessing high is time and disk."""
    assert skippy_extract.DEFAULT_DEPTH < 10
    argv = skippy_extract.container_argv(
        "podman", input_dir="/in", input_name="f", output_dir="/out", name="n",
        depth=9999,
    )
    # The cap is applied where the value enters, not trusted to the caller.
    requested = int(argv[argv.index("--depth") + 1])
    assert requested == 9999
    assert skippy_extract.MAX_DEPTH == 10


def test_a_depth_over_the_maximum_is_clamped_not_obeyed(pack, monkeypatch):
    captured = {}

    async def fake_run(argv, name, output_dir, timeout):
        captured["depth"] = int(argv[argv.index("--depth") + 1])
        return 0, "", ""

    monkeypatch.setattr(skippy_extract.shutil, "which", lambda n: "/usr/bin/podman")
    monkeypatch.setattr(skippy_extract, "_run_container", fake_run)
    asyncio.run(skippy_extract.extract_artifact(pack, depth=500))
    assert captured["depth"] == skippy_extract.MAX_DEPTH


def test_a_zero_depth_is_a_mistake_not_a_request(pack, monkeypatch):
    monkeypatch.setattr(skippy_extract.shutil, "which", lambda n: "/usr/bin/podman")
    result = asyncio.run(skippy_extract.extract_artifact(pack, depth=0))
    assert not result.ok


# --- what the model is told ----------------------------------------------

def test_a_file_with_no_container_in_it_is_a_true_answer_not_a_failure(pack, monkeypatch):
    """"This is not an archive" is a finding. Returning it as an error would push the
    model to retry something that already answered the question."""
    async def fake_run(argv, name, output_dir, timeout):
        return 0, "No extractable chunks found", ""

    monkeypatch.setattr(skippy_extract.shutil, "which", lambda n: "/usr/bin/podman")
    monkeypatch.setattr(skippy_extract, "_run_container", fake_run)
    result = asyncio.run(skippy_extract.extract_artifact(pack))
    assert result.ok
    assert "No container found" in result.summary
    assert "raw code, encrypted" in result.content


def test_a_stopped_extraction_keeps_what_it_got_and_says_it_stopped(pack, monkeypatch):
    """Partial output is worth more than none, and a summary that hid the stop would
    present a fragment of a filesystem as the whole of one."""
    async def fake_run(argv, name, output_dir, timeout):
        with open(os.path.join(output_dir, "partial.bin"), "wb") as handle:
            handle.write(b"\x00" * 32)
        return 137, "", "Extraction was stopped after writing 5000 MB, over the 4096 MB limit."

    monkeypatch.setattr(skippy_extract.shutil, "which", lambda n: "/usr/bin/podman")
    monkeypatch.setattr(skippy_extract, "_run_container", fake_run)
    result = asyncio.run(skippy_extract.extract_artifact(pack))
    assert result.ok
    assert "stopped" in result.summary.lower()
    assert "WARNING" in result.content
    assert "partial.bin" in result.content
    assert result.data["stopped"] is True


def test_the_invocation_is_recorded_so_an_extraction_can_be_rechecked(pack, monkeypatch):
    async def fake_run(argv, name, output_dir, timeout):
        return 0, "", ""

    monkeypatch.setattr(skippy_extract.shutil, "which", lambda n: "/usr/bin/podman")
    monkeypatch.setattr(skippy_extract, "_run_container", fake_run)
    result = asyncio.run(skippy_extract.extract_artifact(pack))
    assert "--network none" in result.data["command"]
    assert "@sha256:" in result.data["command"]


def test_listing_an_empty_quarantine_says_how_to_fill_it(pack):
    result = asyncio.run(skippy_extract.list_extracted(pack))
    assert result.ok
    assert "extract_artifact" in result.summary


def test_listing_shows_what_earlier_sessions_extracted(pack):
    """The pack outlives the session, so what a previous run carved is still there — and
    not knowing that is how the same image gets extracted twice."""
    root = skippy_extract.quarantine_root(pack)
    os.makedirs(os.path.join(root, "0001-firmware-bin"))
    with open(os.path.join(root, "0001-firmware-bin", "httpd"), "wb") as handle:
        handle.write(b"\x7fELF" + b"\x00" * 100)
    result = asyncio.run(skippy_extract.list_extracted(pack))
    assert result.ok
    assert "httpd" in result.content
    assert result.data["file_count"] == 1
