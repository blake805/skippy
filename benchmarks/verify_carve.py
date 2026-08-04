"""Check that unblob actually works inside the restrictions ADR 0019 imposes.

The tests in `tests/test_extract.py` assert the invocation, because the container flags
are what silently regress and CI has no VM. They cannot assert that unblob still functions
with a read-only root filesystem, no capabilities and no network — that needs a real run,
and this is it.

    sh benchmarks/make_carve_fixtures.sh /tmp/carve
    python benchmarks/verify_carve.py --fixtures /tmp/carve

Each check prints PASS or FAIL and the reason. Exits non-zero if any failed, so it can be
run before touching the container flags again.
"""

import argparse
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skippy_extract
import skippy_re
import skippy_rizin

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        for line in str(detail).strip().splitlines()[:6]:
            print(f"          {line}")
    return ok


def pack_for(target: str, notes: str) -> object:
    return skippy_re.open_pack(notes, target=target, title="carve check")


async def check_firmware(fixtures: str, notes: str) -> None:
    print("\n[1] a container that does not start at offset zero")
    target = os.path.join(fixtures, "firmware.bin")
    pack = pack_for(target, notes)
    result = await skippy_extract.extract_artifact(pack)

    check("extraction ran at all", result.ok, result.summary)
    # The whole point of carving rather than untarring: the gzip is at offset 1024.
    check(
        "found the embedded archive",
        "gzip" in " ".join(result.data.get("formats", [])).lower()
        or result.data.get("file_count", 0) > 0,
        result.summary,
    )
    listing = await skippy_extract.list_extracted(pack)
    body = listing.content
    check("recovered the payload's files", "config.ini" in body or "agent" in body, body[:400])

    # If --read-only broke unblob this is where it shows: the run would fail with a write
    # error rather than produce files.
    check(
        "unblob works with a read-only root filesystem",
        result.data.get("file_count", 0) > 0,
        result.summary,
    )

    # Landlock is the layer that covers third-party extractors, and the reason for the
    # container in the first place. Inside a Linux guest it should engage.
    quarantined = result.data.get("quarantine", "")
    check(
        "sandboxing was not skipped (Landlock engaged)",
        "unavailable on this system" not in result.content.lower(),
        "unblob reports sandboxing unavailable — check the guest kernel",
    )

    # Extracted files have to be readable afterwards or the chain stops here.
    root = skippy_extract.quarantine_root(pack)
    readable = True
    unreadable = ""
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            if not os.path.islink(path) and not os.access(path, os.R_OK):
                readable = False
                unreadable = path
                break
    check("extracted files are readable by us, not root", readable, unreadable)
    return quarantined


async def check_traversal(fixtures: str, notes: str) -> None:
    print("\n[2] an archive that tries to escape the extraction directory")
    sentinel = "/tmp/skippy-escaped.txt"
    if os.path.exists(sentinel):
        os.remove(sentinel)

    target = os.path.join(fixtures, "slip.tar")
    pack = pack_for(target, notes)
    result = await skippy_extract.extract_artifact(pack)

    check("the escape did not reach the host", not os.path.exists(sentinel), sentinel)

    root = os.path.realpath(skippy_extract.quarantine_root(pack))
    escaped = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.realpath(os.path.join(base, name))
            if not full.startswith(root + os.sep):
                escaped.append(full)
    check("nothing was written outside the quarantine", not escaped, "\n".join(escaped))

    # Surfacing this is the point: an image that attempts a traversal has said something
    # about itself, and it is very likely a weakness in whatever built it.
    problems = result.data.get("problems") or []
    check(
        "the blocked traversal is reported to the model",
        bool(problems) or "traversal" in result.content.lower(),
        result.content[:400],
    )


async def check_bomb(fixtures: str, notes: str) -> None:
    print("\n[3] a decompression bomb (cap lowered so it fires quickly)")
    target = os.path.join(fixtures, "bomb.gz")
    pack = pack_for(target, notes)

    original = skippy_extract.MAX_OUTPUT_BYTES
    # 8 MB against a payload that expands to 512 MB, so the watchdog has to fire. The
    # real cap is 4 GB, which would take too long to be a useful check.
    skippy_extract.MAX_OUTPUT_BYTES = 8 * 1024 * 1024
    try:
        result = await skippy_extract.extract_artifact(pack)
    finally:
        skippy_extract.MAX_OUTPUT_BYTES = original

    check("the bomb was stopped", bool(result.data.get("stopped")), result.summary)
    check("the model is told it was stopped", "stopped" in result.summary.lower(), result.summary)
    size = result.data.get("bytes", 0)
    check(
        "output stayed near the cap rather than filling the disk",
        size < 200 * 1024 * 1024,
        f"{size:,} bytes written",
    )


async def check_chain(fixtures: str, notes: str) -> None:
    print("\n[4] the chain: read code that came out of the image")
    if not skippy_rizin.available():
        check("rizin available for the chain check", False, "pinned build missing")
        return

    target = os.path.join(fixtures, "firmware.bin")
    pack = pack_for(target, notes)
    root = skippy_extract.quarantine_root(pack)

    agent = ""
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name == "agent":
                agent = os.path.relpath(os.path.join(base, name), root)
                break
    if not agent:
        check("found the extracted executable", False, "no 'agent' in the quarantine")
        return
    check("found the extracted executable", True, agent)

    listed = await skippy_rizin.list_symbols(pack, contains="check", file=agent)
    check("symbols read from an extracted file", listed.ok and "check_token" in listed.content,
          listed.as_observation()[:300])

    decompiled = await skippy_rizin.decompile(pack, "check_token", file=agent)
    check("an extracted function decompiles", decompiled.ok, decompiled.summary)
    if decompiled.ok:
        # The planted string is what proves this is the real function rather than a
        # plausible shape.
        check(
            "the decompilation contains the planted comparison",
            "strcmp" in decompiled.content or "PROVISIONING" in decompiled.content,
            decompiled.content[:400],
        )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", default="/tmp/carve")
    parser.add_argument("--keep", action="store_true", help="keep the notes root")
    args = parser.parse_args()

    if not skippy_extract.available():
        print("No container runtime reachable. Start one first:")
        print("  podman machine start")
        return 2
    print(f"runtime: {skippy_extract.runtime_path()}")
    print(f"image:   {skippy_extract.UNBLOB_IMAGE}")

    notes = tempfile.mkdtemp(prefix="skippy-carve-")
    try:
        await check_firmware(args.fixtures, notes)
        await check_traversal(args.fixtures, notes)
        await check_bomb(args.fixtures, notes)
        await check_chain(args.fixtures, notes)
    finally:
        if args.keep:
            print(f"\nnotes kept at {notes}")
        else:
            shutil.rmtree(notes, ignore_errors=True)

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
