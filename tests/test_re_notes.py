"""Reverse-engineering note packs.

In an RE session nothing is written to the target, so the notes are the only
product. That makes two properties load-bearing: a finding must carry enough to be
rechecked later, and a conclusion that was later retracted must never read as
current. Most of what follows is those two.
"""

import json
import os

import pytest

import skippy_re
from skippy_re import NotesError, note_finding, open_pack, read_notes


@pytest.fixture
def notes_dir(tmp_path):
    return str(tmp_path / "notes")


@pytest.fixture
def pack(notes_dir):
    return open_pack(notes_dir, target="/opt/targets/libfoo.dylib", title="libfoo")


def record(pack, **kwargs):
    """A valid finding, with fields overridable per test."""
    args = {
        "kind": "structure",
        "title": "Header is 32 bytes",
        "body": "The load commands start at +0x20.",
        "evidence": "otool -h shows sizeofcmds 0x20",
        "confidence": "confirmed",
    }
    args.update(kwargs)
    return note_finding(pack, **args)


# --- evidence is mandatory, and the refusal has to be followable ---

def test_an_asserted_finding_without_evidence_is_refused(pack):
    result = record(pack, evidence="")
    assert not result.ok
    assert "evidence" in result.summary


def test_the_advice_in_the_evidence_refusal_actually_works(pack):
    """The refusal tells the model what to do instead. If following that advice hits
    the same refusal, the model has no way out and burns its remaining steps
    alternating between two rejected calls.

    This failed before: the message named both 'hypothesis' and 'question' as the
    way to record something unverified, but 'hypothesis' still required evidence and
    came back with the identical text.
    """
    refusal = record(pack, evidence="").summary

    # Whichever kinds the message recommends must accept a finding with no evidence.
    for kind in ("hypothesis", "question"):
        if f"'{kind}'" not in refusal:
            continue
        followed = note_finding(
            pack,
            kind=kind,
            title=f"Possibly RC4 ({kind})",
            body="The key schedule resembles RC4. Comparing the S-box init would confirm it.",
            evidence="",
            confidence="speculative",
        )
        assert followed.ok, (
            f"the refusal recommends kind '{kind}' but rejects it: {followed.summary}"
        )


def test_a_question_needs_no_evidence(pack):
    result = note_finding(
        pack, kind="question", title="What writes the trailer?",
        body="Something appends 16 bytes after the last section.",
        evidence="", confidence="speculative",
    )
    assert result.ok


def test_a_finding_still_needs_a_body(pack):
    assert not record(pack, body="").ok


def test_a_finding_needs_a_title(pack):
    assert not record(pack, title="").ok


# --- confidence is mandatory and separate from evidence ---

def test_confidence_is_required(pack):
    result = record(pack, confidence="")
    assert not result.ok
    assert "speculative" in result.summary and "confirmed" in result.summary


def test_an_invented_confidence_level_is_refused(pack):
    assert not record(pack, confidence="pretty sure").ok


@pytest.mark.parametrize("level", skippy_re.CONFIDENCE)
def test_every_documented_confidence_level_is_accepted(pack, level):
    assert record(pack, confidence=level, title=f"Finding at {level}").ok


# --- the kind taxonomy is closed ---

@pytest.mark.parametrize("kind", sorted(skippy_re.KINDS))
def test_every_documented_kind_is_accepted(pack, kind):
    # Some kinds carry an extra mandatory field — `weakness` needs a severity — so
    # the fixture supplies whatever the kind under test requires. The point of the
    # test is that no kind in the taxonomy is unusable, not that they are uniform.
    extra = {"severity": "medium"} if kind in skippy_re.KINDS_REQUIRING_SEVERITY else {}
    assert record(pack, kind=kind, title=f"A {kind} finding", **extra).ok


def test_an_invented_kind_is_refused_and_the_real_ones_listed(pack):
    result = record(pack, kind="reverse_engineering_note")
    assert not result.ok
    # Listing them is what stops the model inventing a second one.
    for kind in skippy_re.KINDS:
        assert kind in result.summary


# --- append-only, with supersede rather than overwrite ---

def test_superseding_does_not_touch_the_earlier_finding(pack):
    first = record(pack, title="Field at 0x10 is a length", confidence="likely")
    path = first.data["finding"]["path"]
    before = open(path, encoding="utf-8").read()

    second = record(
        pack, title="Field at 0x10 is a checksum",
        body="It varies with content, so it is not a length.",
        evidence="xxd at 0x10 changes when a later byte changes",
        supersedes=first.data["finding"]["id"],
    )
    assert second.ok
    # Being wrong and then right is the normal shape of the work; the record of
    # having been wrong is itself worth keeping.
    assert open(path, encoding="utf-8").read() == before


def test_a_superseded_finding_is_marked_in_the_rollup(pack):
    first = record(pack, title="Field at 0x10 is a length")
    record(pack, title="Field at 0x10 is a checksum", supersedes=first.data["finding"]["id"])
    assert "superseded" in pack.write_index()


def test_a_superseded_finding_is_marked_when_read_by_kind(pack):
    """Otherwise the model reads a retracted conclusion as current and cites it.

    The supersede relationship lives on the *newer* finding, so nothing in the older
    file says it was retracted — which means any read path that returns findings has
    to add that itself.
    """
    first = record(pack, title="Field at 0x10 is a length", confidence="likely")
    record(
        pack, title="Field at 0x10 is a checksum",
        evidence="xxd at 0x10 tracks content",
        supersedes=first.data["finding"]["id"],
    )
    result = read_notes(pack, kind="structure")
    assert result.ok
    assert "SUPERSEDED" in result.content.upper()


def test_a_superseded_finding_is_marked_when_read_by_id(pack):
    first = record(pack, title="Field at 0x10 is a length")
    record(pack, title="Field at 0x10 is a checksum", supersedes=first.data["finding"]["id"])
    result = read_notes(pack, finding_id=first.data["finding"]["id"])
    assert "SUPERSEDED" in result.content.upper()


def test_superseding_an_unknown_id_is_refused(pack):
    record(pack, title="Something")
    result = record(pack, title="Correction", supersedes="0099")
    assert not result.ok
    assert "0099" in result.summary
    assert "0001" in result.summary  # the known ids, so it can pick the right one


def test_several_findings_can_be_superseded_at_once(pack):
    a = record(pack, title="Guess one", confidence="speculative")
    b = record(pack, title="Guess two", confidence="speculative")
    combined = record(
        pack, title="Both were the same field",
        supersedes=f"{a.data['finding']['id']},{b.data['finding']['id']}",
    )
    assert combined.ok
    index = pack.write_index()
    assert index.count("superseded") == 2


# --- ids and filenames ---

def test_finding_ids_increment(pack):
    ids = [record(pack, title=f"Finding {n}").data["finding"]["id"] for n in range(3)]
    assert ids == ["0001", "0002", "0003"]


def test_ids_keep_incrementing_after_reopening_the_pack(notes_dir):
    first = open_pack(notes_dir, target="libfoo.dylib")
    record(first, title="From the first session")
    again = open_pack(notes_dir, target="libfoo.dylib")
    assert record(again, title="From the second session").data["finding"]["id"] == "0002"


def test_a_pack_is_keyed_by_target_so_a_later_session_accumulates(notes_dir):
    """Rediscovering last week's conclusions is the most common waste in RE."""
    first = open_pack(notes_dir, target="/opt/libfoo.dylib")
    record(first, title="Established last week")
    later = open_pack(notes_dir, target="/opt/libfoo.dylib")
    assert later.pack_id == first.pack_id
    assert "Established last week" in read_notes(later).content


def test_different_targets_get_different_packs(notes_dir):
    a = open_pack(notes_dir, target="/opt/libfoo.dylib")
    b = open_pack(notes_dir, target="/opt/libbar.dylib")
    assert a.pack_id != b.pack_id


def test_reopening_does_not_relabel_an_existing_investigation(notes_dir):
    first = open_pack(notes_dir, target="/opt/libfoo.dylib", title="Original title")
    open_pack(notes_dir, pack_id=first.pack_id, title="Something else")
    meta = json.load(open(os.path.join(first.dir, "pack.json"), encoding="utf-8"))
    assert meta["title"] == "Original title"


def test_reading_a_pack_does_not_change_its_updated_timestamp(notes_dir):
    """'updated' means "when a finding was last recorded". If opening or reading a
    pack moved it, it would stop being usable for finding the live investigation."""
    pack = open_pack(notes_dir, target="/opt/libfoo.dylib")
    record(pack, title="A finding")
    meta_path = os.path.join(pack.dir, "pack.json")
    stamped = json.load(open(meta_path, encoding="utf-8"))["updated"]

    read_notes(open_pack(notes_dir, target="/opt/libfoo.dylib"))
    assert json.load(open(meta_path, encoding="utf-8"))["updated"] == stamped


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".", ".."])
def test_an_unsafe_pack_id_is_refused(notes_dir, bad):
    with pytest.raises(NotesError):
        open_pack(notes_dir, pack_id=bad)


def test_an_omitted_pack_id_falls_back_to_a_named_default(notes_dir):
    """Not an error: a pack with no target still needs somewhere to go."""
    assert open_pack(notes_dir).pack_id == "investigation"


# --- front matter survives awkward titles ---

@pytest.mark.parametrize("title", [
    "Offset 0x10: a length prefix",
    'The "magic" bytes',
    "It's a checksum",
    "A title with #hash and: colon",
    "Multi\nline\ntitle",
    "  leading and trailing  ",
])
def test_a_title_with_yaml_metacharacters_round_trips(pack, title):
    """The front matter is written and parsed by this module, so the two have to
    agree about quoting or the pack becomes unreadable by its own reader."""
    result = record(pack, title=title)
    assert result.ok
    read_back = read_notes(pack, finding_id=result.data["finding"]["id"])
    assert read_back.ok
    expected = " ".join(title.split())
    assert expected in " ".join(read_back.content.split())


def test_a_location_is_recorded_when_given(pack):
    result = record(pack, location="0x1a40")
    read_back = read_notes(pack, finding_id=result.data["finding"]["id"])
    assert "0x1a40" in read_back.content


# --- reading ---

def test_reading_with_no_arguments_returns_the_rollup(pack):
    record(pack, title="First thing")
    record(pack, kind="question", title="What is the trailer?", evidence="")
    result = read_notes(pack)
    assert result.ok
    assert "First thing" in result.content
    # Open questions are surfaced separately; most of a session is what is not known.
    assert "Open questions" in result.content
    assert result.data["findings"] == 2


def test_reading_an_empty_pack_says_so_rather_than_failing(pack):
    result = read_notes(pack)
    assert result.ok
    assert "None yet" in result.content


def test_reading_one_finding_by_id(pack):
    record(pack, title="First")
    second = record(pack, title="Second")
    result = read_notes(pack, finding_id=second.data["finding"]["id"])
    assert "Second" in result.content
    assert "First" not in result.content


def test_an_unpadded_id_still_resolves(pack):
    """The model will write 1, not 0001."""
    record(pack, title="First")
    assert read_notes(pack, finding_id="1").ok


def test_an_unknown_id_lists_the_known_ones(pack):
    record(pack, title="First")
    result = read_notes(pack, finding_id="0042")
    assert not result.ok
    assert "0001" in result.summary


def test_reading_by_kind_filters(pack):
    record(pack, kind="structure", title="A structure finding")
    record(pack, kind="constant", title="A constant finding")
    result = read_notes(pack, kind="constant")
    assert "A constant finding" in result.content
    assert "A structure finding" not in result.content


def test_reading_an_unused_kind_is_not_an_error(pack):
    record(pack, kind="structure", title="Only structure here")
    result = read_notes(pack, kind="symbol")
    assert result.ok
    assert "No 'symbol' findings" in result.summary


def test_reading_an_invalid_kind_is_refused(pack):
    assert not read_notes(pack, kind="nonsense").ok


# --- durability ---

def test_findings_are_plain_files_a_person_can_read(pack):
    result = record(pack, title="Header is 32 bytes")
    path = result.data["finding"]["path"]
    assert path.endswith(".md")
    text = open(path, encoding="utf-8").read()
    assert "# Header is 32 bytes" in text
    assert "## Evidence" in text
    assert "otool -h" in text


def test_a_corrupt_pack_json_does_not_lose_the_findings(pack, notes_dir):
    """The findings are the valuable part and are stored separately from the metadata."""
    record(pack, title="Worth keeping")
    with open(os.path.join(pack.dir, "pack.json"), "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")

    reopened = open_pack(notes_dir, pack_id=pack.pack_id)
    assert "Worth keeping" in read_notes(reopened).content


def test_the_index_can_be_deleted_and_rebuilt(pack):
    record(pack, title="Still here")
    os.remove(os.path.join(pack.dir, "index.md"))
    assert "Still here" in read_notes(pack).content


def test_a_stray_file_in_the_findings_directory_is_ignored(pack):
    record(pack, title="Real finding")
    with open(os.path.join(pack.findings_dir, "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("not a finding")
    assert read_notes(pack).data["findings"] == 1


def test_listing_packs_reports_what_is_there(notes_dir):
    first = open_pack(notes_dir, target="/opt/libfoo.dylib", title="foo")
    record(first, title="One")
    open_pack(notes_dir, target="/opt/libbar.dylib", title="bar")

    packs = {p["pack_id"]: p for p in skippy_re.list_packs(notes_dir)}
    assert len(packs) == 2
    assert packs[first.pack_id]["findings"] == 1
    assert packs[first.pack_id]["target"] == "/opt/libfoo.dylib"


def test_listing_a_missing_notes_root_is_empty_not_an_error(tmp_path):
    assert skippy_re.list_packs(str(tmp_path / "nope")) == []


# --- bounds ---

def test_an_enormous_body_is_capped(pack):
    result = record(pack, body="x" * 40_000)
    assert result.ok
    text = open(result.data["finding"]["path"], encoding="utf-8").read()
    assert len(text) < skippy_re.MAX_BODY_CHARS + skippy_re.MAX_EVIDENCE_CHARS + 2_000


def test_an_enormous_evidence_block_is_capped(pack):
    result = record(pack, evidence="y" * 40_000)
    assert result.ok
    text = open(result.data["finding"]["path"], encoding="utf-8").read()
    assert len(text) < skippy_re.MAX_BODY_CHARS + skippy_re.MAX_EVIDENCE_CHARS + 2_000


# --- one pack per target, not per filename ---
#
# The pack id used to be the slugified target, so two of our own products that both
# ship a `firmware.bin` landed in one pack. That is the worst available failure,
# because nothing looks wrong: the second session opens the first product's findings
# as established prior knowledge and appends to them.

def test_two_targets_with_the_same_basename_do_not_share_a_pack(notes_dir, tmp_path):
    (tmp_path / "gate").mkdir()
    (tmp_path / "sensor").mkdir()
    gate = tmp_path / "gate" / "firmware.bin"
    sensor = tmp_path / "sensor" / "firmware.bin"
    gate.write_bytes(b"gate build")
    sensor.write_bytes(b"sensor build")

    first = open_pack(notes_dir, target=str(gate))
    second = open_pack(notes_dir, target=str(sensor))
    assert first.pack_id != second.pack_id

    record(first, title="Gate header is 32 bytes")
    # The whole point: the second investigation must not open the first one's work.
    assert "Gate header" not in read_notes(second).content


def test_the_same_target_reopens_the_same_pack(notes_dir):
    """Accumulating across sessions is the reason packs are keyed by target at all."""
    first = open_pack(notes_dir, target="/opt/products/gate/firmware.bin")
    again = open_pack(notes_dir, target="/opt/products/gate/firmware.bin")
    assert first.pack_id == again.pack_id


def test_the_pack_id_still_reads_as_the_target(notes_dir):
    """A directory of packs has to be navigable by eye, so the digest is a suffix
    rather than the whole id."""
    pack = open_pack(notes_dir, target="/opt/products/gate/firmware.bin")
    assert pack.pack_id.startswith("firmware-bin-")


def test_a_target_named_only_by_title_still_gets_a_pack(notes_dir):
    assert open_pack(notes_dir, title="The mystery board").pack_id


# --- the target can change underneath a pack ---

def test_a_rebuilt_target_is_flagged_on_every_read_path(notes_dir, tmp_path):
    """Same reasoning as marking a superseded finding: findings about bytes that have
    since changed read as current, and the model has no way to notice by itself."""
    target = tmp_path / "firmware.bin"
    target.write_bytes(b"version one")

    pack = open_pack(notes_dir, target=str(target))
    result = record(pack, title="Header is 32 bytes")
    assert not pack.target_changed

    target.write_bytes(b"version two, rebuilt")
    reopened = open_pack(notes_dir, target=str(target))
    assert reopened.target_changed

    # All three read paths, because the first version of the supersede banner only
    # covered the index and handed retracted findings back unmarked everywhere else.
    assert "have changed" in read_notes(reopened).content
    assert "have changed" in read_notes(reopened, finding_id=result.data["finding"]["id"]).content
    assert "have changed" in read_notes(reopened, kind="structure").content


def test_an_unchanged_target_is_not_flagged(notes_dir, tmp_path):
    target = tmp_path / "firmware.bin"
    target.write_bytes(b"unchanged")
    open_pack(notes_dir, target=str(target))
    reopened = open_pack(notes_dir, target=str(target))
    assert not reopened.target_changed
    assert "have changed" not in read_notes(reopened).content


def test_the_recorded_digest_is_never_refreshed(notes_dir, tmp_path):
    """Refreshing it would silently adopt whatever bytes are there now, which is
    exactly the event this exists to report."""
    target = tmp_path / "firmware.bin"
    target.write_bytes(b"version one")
    first = open_pack(notes_dir, target=str(target))
    original = first.meta["target_digest"]

    target.write_bytes(b"version two")
    reopened = open_pack(notes_dir, target=str(target))
    assert reopened.meta["target_digest"] == original


def test_a_target_that_is_not_a_file_is_not_an_error(notes_dir):
    """A target may name a board or a device rather than a path, and that is normal."""
    pack = open_pack(notes_dir, target="the ESP32 on the bench")
    assert not pack.target_changed
    assert record(pack, title="It boots").ok


def test_a_large_target_is_fingerprinted_by_sampling(notes_dir, tmp_path, monkeypatch):
    """A pack is opened before the first step of every run, so a flash image must not
    be read end to end just to notice it is the same one."""
    monkeypatch.setattr(skippy_re, "DIGEST_FULL_LIMIT", 1_024)
    monkeypatch.setattr(skippy_re, "DIGEST_SAMPLE_BYTES", 256)
    target = tmp_path / "flash.bin"
    target.write_bytes(b"A" * 4_096)

    pack = open_pack(notes_dir, target=str(target))
    assert pack.meta["digest_method"] == "sha256-sampled"

    # Sampling still has to notice a change at either end.
    target.write_bytes(b"B" + b"A" * 4_095)
    assert open_pack(notes_dir, target=str(target)).target_changed


def test_digests_computed_differently_are_never_compared(notes_dir, tmp_path, monkeypatch):
    """Comparing a sampled digest against a full one would report every target as
    changed, which trains the reader to ignore the warning."""
    target = tmp_path / "flash.bin"
    target.write_bytes(b"A" * 4_096)
    open_pack(notes_dir, target=str(target))

    monkeypatch.setattr(skippy_re, "DIGEST_FULL_LIMIT", 1_024)
    assert not open_pack(notes_dir, target=str(target)).target_changed


# --- the loop's command log ---
#
# Findings are written when the model decides to write them, and the first live run
# decided to write them all at the end. The commands are recorded mechanically so the
# evidence survives regardless of how the run ends.

def test_a_logged_command_is_a_plain_file_holding_its_output(pack):
    record_ = pack.log_command("otool -h target", output="magic 0xfeedfacf", cwd="/opt",
                               exit_code=0, ok=True)
    text = open(record_["path"], encoding="utf-8").read()
    assert "otool -h target" in text
    assert "magic 0xfeedfacf" in text
    assert "exit_code: 0" in text


def test_commands_are_numbered_in_the_order_they_ran(pack):
    for index in range(3):
        pack.log_command(f"strings -n {index} target", output=str(index))
    ids = [os.path.basename(p).split("-", 1)[0] for p in pack.command_files()]
    assert ids == ["0001", "0002", "0003"]


def test_a_blank_command_is_not_logged(pack):
    assert pack.log_command("") == {}
    assert pack.command_files() == []


def test_enormous_command_output_is_capped(pack):
    """An objdump region is exactly the case that matters, and exactly the one that
    would otherwise fill the memory root."""
    record_ = pack.log_command("objdump -d target", output="x" * 200_000)
    text = open(record_["path"], encoding="utf-8").read()
    assert len(text) < skippy_re.MAX_COMMAND_OUTPUT_CHARS + 2_000


def test_the_command_count_is_reported_alongside_the_findings(pack):
    pack.log_command("file target", output="Mach-O 64-bit executable")
    record(pack, title="It is a Mach-O")
    result = read_notes(pack)
    assert result.data["commands"] == 1
    assert "Commands logged: 1" in result.content


def test_a_stray_file_in_the_commands_directory_is_ignored(pack):
    pack.log_command("file target", output="x")
    with open(os.path.join(pack.commands_dir, "scratch.txt"), "w", encoding="utf-8") as handle:
        handle.write("not a command")
    assert len(pack.command_files()) == 1


# --- weaknesses carry a severity ---

def test_a_weakness_without_a_severity_is_refused(pack):
    result = record(pack, kind="weakness", title="Stack buffer is unchecked")
    assert not result.ok
    assert "severity" in result.summary
    # The refusal has to name the options, or the model guesses again.
    for level in skippy_re.SEVERITY:
        assert level in result.summary


def test_the_advice_in_the_severity_refusal_actually_works(pack):
    """Same rule as the evidence refusal: a message recommending an action that is
    then rejected leaves the model alternating between two refusals."""
    refusal = record(pack, kind="weakness", title="Unchecked copy").summary
    for level in skippy_re.SEVERITY:
        if level not in refusal:
            continue
        followed = record(pack, kind="weakness", title=f"Unchecked copy ({level})",
                          severity=level)
        assert followed.ok, f"the refusal offers '{level}' but rejects it: {followed.summary}"


def test_an_invented_severity_is_refused_as_a_misspelling(pack):
    """Reported as a bad value rather than as a missing one, which is a different fix."""
    result = record(pack, kind="weakness", title="x", severity="sev1")
    assert not result.ok
    assert "sev1" in result.summary


def test_a_weakness_still_needs_evidence(pack):
    """A weakness drives a change to our own code, so it is the last kind that should
    be recordable on a hunch."""
    assert not record(pack, kind="weakness", severity="high", evidence="").ok


def test_severity_and_confidence_are_recorded_separately(pack):
    """A speculative critical and a confirmed critical are different work, and showing
    severity alone is how a guess acquires a deadline."""
    result = record(pack, kind="weakness", title="Unchecked length field",
                    severity="critical", confidence="speculative")
    assert result.ok
    text = open(result.data["finding"]["path"], encoding="utf-8").read()
    assert "severity: critical" in text
    assert "confidence: speculative" in text


@pytest.mark.parametrize("level", skippy_re.SEVERITY)
def test_every_documented_severity_is_accepted(pack, level):
    assert record(pack, kind="weakness", title=f"A {level} weakness", severity=level).ok


def test_weaknesses_lead_the_index_worst_first(pack):
    record(pack, kind="weakness", title="Minor logging leak", severity="low")
    record(pack, kind="weakness", title="Unauthenticated firmware update", severity="critical")
    record(pack, kind="structure", title="Header is 32 bytes")

    content = read_notes(pack).content
    assert content.index("## Weaknesses") < content.index("## Findings")
    assert content.index("Unauthenticated firmware update") < content.index("Minor logging leak")


def test_a_superseded_weakness_leaves_the_weakness_list(pack):
    first = record(pack, kind="weakness", title="Suspected overflow", severity="high")
    record(pack, kind="weakness", title="Not an overflow after all; bounds are checked",
           severity="low", supersedes=first.data["finding"]["id"])

    weaknesses = read_notes(pack).content.split("## Findings")[0]
    assert "Suspected overflow" not in weaknesses
