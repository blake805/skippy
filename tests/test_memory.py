"""Project memory.

The point of this module is that a new session continues prior work instead of
starting blind, so the tests are mostly about two things: the record surviving in a
form the next session can use, and memory admitting when it has gone stale. The
second matters more. Useless memory wastes context; confidently wrong memory sends
the next session in the wrong direction with the authority of project history behind
it.
"""

import json
import os
import subprocess

import pytest

import skippy_memory
from skippy_memory import open_project, record_decision


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "projects")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "calc"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    return root


@pytest.fixture
def memory(store, repo):
    return open_project(root=store, workspace_roots=[str(repo)])


# --- project identity ---

def test_the_same_roots_reopen_the_same_project(store, repo):
    """Nobody has to remember a project name for continuity to work."""
    first = open_project(root=store, workspace_roots=[str(repo)])
    again = open_project(root=store, workspace_roots=[str(repo)])
    assert first.project_id == again.project_id


def test_root_order_does_not_create_a_second_project(store, tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    a = open_project(root=store, workspace_roots=[str(tmp_path / "api"), str(tmp_path / "web")])
    b = open_project(root=store, workspace_roots=[str(tmp_path / "web"), str(tmp_path / "api")])
    assert a.project_id == b.project_id


def test_several_roots_are_one_project(store, tmp_path):
    """Cross-repo work is the reason for having several roots, so it is one
    investigation rather than one project per repo."""
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    memory = open_project(root=store, workspace_roots=[str(tmp_path / "api"), str(tmp_path / "web")])
    assert "api" in memory.project_id and "web" in memory.project_id


def test_different_projects_stay_separate(store, tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    a = open_project(root=store, workspace_roots=[str(tmp_path / "one")])
    b = open_project(root=store, workspace_roots=[str(tmp_path / "two")])
    assert a.project_id != b.project_id
    a.record_session("Task in one", "finished", "did a thing")
    assert b.sessions() == []


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".", ".."])
def test_an_unsafe_project_id_is_refused(store, bad):
    with pytest.raises(skippy_memory.MemoryError_):
        open_project(root=store, project_id=bad)


def test_no_roots_still_gets_a_usable_project(store):
    assert open_project(root=store, workspace_roots=[]).project_id == "unscoped"


# --- sessions are recorded, including the failures ---

def test_a_finished_run_is_recorded(memory):
    memory.record_session(
        task="Add retry to the client", status="finished",
        summary="Added retry with backoff.", files_changed=["calc/ops.py"], steps=7,
    )
    records = memory.sessions()
    assert len(records) == 1
    assert records[0]["status"] == "finished"
    assert records[0]["files_changed"] == ["calc/ops.py"]


@pytest.mark.parametrize("status", ["max_steps", "stopped_without_finish", "cancelled", "failed"])
def test_a_run_that_did_not_finish_is_still_recorded(memory, status):
    """The most useful thing the next session can know is that a migration was left
    half-done. A save-on-success rule would throw exactly that away."""
    memory.record_session(task="Big migration", status=status, summary="Got halfway.")
    assert memory.sessions()[0]["status"] == status


def test_sessions_come_back_newest_first(memory):
    for n in range(3):
        memory.record_session(task=f"Task {n}", status="finished", summary=f"Did {n}")
    summaries = [s["summary"] for s in memory.sessions()]
    assert summaries == ["Did 2", "Did 1", "Did 0"]


def test_two_runs_in_the_same_second_do_not_overwrite_each_other(memory):
    first = memory.record_session(task="a", status="finished", summary="first")
    second = memory.record_session(task="b", status="finished", summary="second")
    assert first != second
    assert len(memory.sessions()) == 2


def test_the_commit_is_recorded_with_the_session(store, repo):
    """So a later reader can tell how much has happened since."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    memory = open_project(root=store, workspace_roots=[str(repo)])
    memory.record_session(task="t", status="finished", summary="s")
    assert memory.sessions()[0]["commit"]


def test_a_project_outside_git_records_no_commit_rather_than_failing(memory):
    memory.record_session(task="t", status="finished", summary="s")
    assert memory.sessions()[0]["commit"] == ""


def test_only_recent_sessions_are_loaded(memory):
    for n in range(20):
        memory.record_session(task=f"t{n}", status="finished", summary=f"s{n}")
    assert len(memory.sessions()) == skippy_memory.RECENT_SESSIONS
    # The rest are still on disk, greppable by hand.
    assert len(os.listdir(memory.sessions_dir)) == 20


# --- chat transcripts: the resume handle, not the summary ---

def test_a_chat_accumulates_turns_across_appends(memory):
    memory.append_chat("chat-1", [
        {"role": "user", "content": "hey skippy"},
        {"role": "assistant", "content": "hey yourself"},
    ])
    memory.append_chat("chat-1", [
        {"role": "user", "content": "still there?"},
        {"role": "assistant", "content": "always"},
    ])
    record = memory.load_chat("chat-1")
    assert [t["role"] for t in record["turns"]] == ["user", "assistant", "user", "assistant"]
    assert record["turns"][-1]["content"] == "always"


def test_a_chat_is_titled_by_its_first_user_message(memory):
    memory.append_chat("chat-1", [
        {"role": "user", "content": "should the bracket be aluminum or titanium?"},
        {"role": "assistant", "content": "depends on the load"},
    ])
    assert memory.load_chat("chat-1")["title"].startswith("should the bracket")


def test_chats_list_most_recently_touched_first(memory):
    memory.append_chat("older", [{"role": "user", "content": "first conversation"}])
    memory.append_chat("newer", [{"role": "user", "content": "second conversation"}])
    memory.append_chat("older", [{"role": "user", "content": "back to the first"}])
    assert [c["chat_id"] for c in memory.chats()][0] == "older"


def test_loading_an_unknown_chat_is_none_not_an_error(memory):
    assert memory.load_chat("never-existed") is None


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", ".hidden"])
def test_an_unsafe_chat_id_is_refused_without_writing(memory, bad):
    assert memory.append_chat(bad, [{"role": "user", "content": "x"}]) is None
    assert memory.chats() == []
    assert memory.load_chat(bad) is None


def test_turns_with_no_content_or_a_strange_role_are_dropped(memory):
    record = memory.append_chat("chat-1", [
        {"role": "system", "content": "not a turn"},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "the real message"},
    ])
    assert [t["content"] for t in record["turns"]] == ["the real message"]


def test_a_transcript_is_capped_oldest_first(memory):
    for n in range(skippy_memory.MAX_CHAT_TURNS + 10):
        memory.append_chat("long", [{"role": "user", "content": f"turn {n}"}])
    record = memory.load_chat("long")
    assert len(record["turns"]) == skippy_memory.MAX_CHAT_TURNS
    assert record["turns"][-1]["content"] == f"turn {skippy_memory.MAX_CHAT_TURNS + 9}"


def test_a_failed_transcript_write_costs_persistence_not_the_turn(memory):
    """The rule that governs the whole store: an unmounted NAS must never take the
    conversation down with it."""
    memory.chats_dir = os.path.join(memory.dir, "meta.json", "impossible")
    assert memory.append_chat("chat-1", [{"role": "user", "content": "x"}]) is None


def test_a_corrupt_transcript_is_skipped_in_the_list(memory):
    memory.append_chat("good", [{"role": "user", "content": "fine"}])
    with open(os.path.join(memory.chats_dir, "bad.json"), "w") as handle:
        handle.write("{ not json")
    assert [c["chat_id"] for c in memory.chats()] == ["good"]


def test_chats_stay_with_their_project(store, tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    a = open_project(root=store, workspace_roots=[str(tmp_path / "one")])
    b = open_project(root=store, workspace_roots=[str(tmp_path / "two")])
    a.append_chat("chat-1", [{"role": "user", "content": "in project one"}])
    assert b.chats() == []
    assert b.load_chat("chat-1") is None


# --- decisions ---

def test_a_decision_needs_reasoning_not_just_a_title(memory):
    """A title says what was chosen; the body is what stops it being undone."""
    assert not record_decision(memory, title="Use retries", body="").ok
    assert not record_decision(memory, title="", body="Because of flakiness").ok
    assert record_decision(memory, title="Use retries", body="The API is flaky.").ok


def test_a_decision_is_a_plain_file_a_person_can_read(memory):
    result = record_decision(
        memory, title="Retries belong in the transport",
        body="Per-call retries duplicated the backoff logic four times.",
        affects="calc/ops.py",
    )
    text = open(result.data["decision"]["path"], encoding="utf-8").read()
    assert "# Retries belong in the transport" in text
    assert "duplicated the backoff" in text
    assert "calc/ops.py" in text


def test_decision_ids_increment_across_sessions(store, repo):
    first = open_project(root=store, workspace_roots=[str(repo)])
    record_decision(first, title="One", body="reason")
    later = open_project(root=store, workspace_roots=[str(repo)])
    assert record_decision(later, title="Two", body="reason").data["decision"]["id"] == "0002"


def test_superseding_does_not_rewrite_the_earlier_decision(memory):
    first = record_decision(memory, title="Use polling", body="Simplest thing.")
    path = first.data["decision"]["path"]
    before = open(path, encoding="utf-8").read()

    second = record_decision(
        memory, title="Use webhooks", body="Polling cost too much quota.",
        supersedes=first.data["decision"]["id"],
    )
    assert second.ok
    # Why something was reversed is often the answer to why the code looks as it does.
    assert open(path, encoding="utf-8").read() == before


def test_superseding_an_unknown_decision_is_refused(memory):
    record_decision(memory, title="One", body="reason")
    result = record_decision(memory, title="Two", body="reason", supersedes="0099")
    assert not result.ok
    assert "0099" in result.summary and "0001" in result.summary


@pytest.mark.parametrize("title", [
    "Offset 0x10: use a length prefix",
    'The "fast" path',
    "It's the transport's job",
    "Tags: [a, b]",
])
def test_a_decision_title_with_yaml_metacharacters_round_trips(memory, title):
    record_decision(memory, title=title, body="reason")
    front = memory.decisions()[-1]["front"]
    assert front["title"] == " ".join(title.split())


# --- staleness: the part that decides whether memory helps or hurts ---

def test_a_decision_about_a_deleted_file_is_marked_out_of_date(memory, repo):
    """Unmarked, this is misinformation with the authority of project history: the
    model reads "retries live in ops.py", believes it, and works from a file that is
    no longer there."""
    record_decision(
        memory, title="Retries live in ops.py", body="Kept next to the transport.",
        affects="calc/ops.py",
    )
    assert "OUT OF DATE" not in memory.opening_context()

    (repo / "calc" / "ops.py").unlink()
    context = memory.opening_context()
    assert "OUT OF DATE" in context
    assert "calc/ops.py" in context


def test_a_decision_about_a_file_that_still_exists_is_not_marked(memory):
    record_decision(memory, title="Ops stay pure", body="No IO in ops.", affects="calc/ops.py")
    assert "OUT OF DATE" not in memory.opening_context()


def test_a_decision_with_no_affects_is_never_marked_stale(memory):
    record_decision(memory, title="Prefer composition", body="A general principle.")
    assert "OUT OF DATE" not in memory.opening_context()


def test_staleness_is_reported_by_recall_too(memory, repo):
    record_decision(
        memory, title="Retries live in ops.py", body="Kept next to the transport.",
        affects="calc/ops.py",
    )
    (repo / "calc" / "ops.py").unlink()
    assert "OUT OF DATE" in memory.recall("retries").content


def test_a_superseded_decision_is_marked_in_recall(memory):
    first = record_decision(memory, title="Use polling for updates", body="Simplest.")
    record_decision(
        memory, title="Use webhooks for updates", body="Polling burned quota.",
        supersedes=first.data["decision"]["id"],
    )
    result = memory.recall("polling updates")
    assert "SUPERSEDED" in result.content


def test_a_superseded_decision_is_not_offered_as_current_context(memory):
    first = record_decision(memory, title="Use polling", body="Simplest.")
    record_decision(
        memory, title="Use webhooks", body="Polling burned quota.",
        supersedes=first.data["decision"]["id"],
    )
    context = memory.opening_context()
    assert "Use webhooks" in context
    assert "Use polling" not in context


# --- the opening context ---

def test_a_brand_new_project_contributes_nothing(memory):
    """No history means no preamble; an empty section is context spent on nothing."""
    assert memory.opening_context() == ""


def test_the_opening_context_carries_prior_work_forward(memory):
    memory.record_session(
        task="Add retry to the client", status="finished",
        summary="Added retry with backoff in the transport.", files_changed=["calc/ops.py"],
    )
    record_decision(memory, title="Retries belong in the transport", body="Avoids duplication.")

    context = memory.opening_context()
    assert "Retries belong in the transport" in context
    assert "backoff" in context
    assert "calc/ops.py" in context


def test_the_opening_context_says_it_is_a_record_not_an_instruction(memory):
    """Otherwise a stale note reads as a directive and the model follows it over what
    the code in front of it actually says."""
    memory.record_session(task="t", status="finished", summary="s")
    context = memory.opening_context()
    assert "not instructions" in context
    assert "the code is right" in context


def test_the_opening_context_is_bounded(memory):
    for n in range(60):
        memory.record_session(
            task=f"Task number {n} " + "x" * 500, status="finished",
            summary="y" * 2_000, files_changed=[f"file{n}.py"],
        )
    for n in range(30):
        record_decision(memory, title=f"Decision {n}", body="z" * 2_000)
    assert len(memory.opening_context()) <= skippy_memory.MAX_CONTEXT_CHARS


def test_conventions_are_carried_forward(memory):
    memory.learn_convention("test command", "python -m pytest -q")
    assert "python -m pytest -q" in memory.opening_context()


def test_learning_the_same_convention_twice_is_not_a_change(memory):
    memory.learn_convention("test command", "pytest")
    first = json.load(open(memory.meta_path, encoding="utf-8"))["updated"]
    memory.learn_convention("test command", "pytest")
    assert json.load(open(memory.meta_path, encoding="utf-8"))["updated"] == first


def test_reading_a_project_does_not_touch_its_timestamp(store, repo):
    """`updated` is how you find the live project among a directory of them."""
    memory = open_project(root=store, workspace_roots=[str(repo)])
    memory.record_session(task="t", status="finished", summary="s")
    stamped = json.load(open(memory.meta_path, encoding="utf-8"))["updated"]

    reopened = open_project(root=store, workspace_roots=[str(repo)])
    reopened.opening_context()
    assert json.load(open(reopened.meta_path, encoding="utf-8"))["updated"] == stamped


# --- recall ---

def test_recall_with_no_query_gives_an_overview(memory):
    memory.record_session(task="t", status="finished", summary="Did the thing")
    result = memory.recall()
    assert result.ok
    assert "Did the thing" in result.content


def test_recall_finds_an_old_decision_the_opening_context_dropped(memory):
    record_decision(memory, title="Serialise with msgpack", body="Protobuf needed a compiler step.")
    for n in range(40):
        record_decision(memory, title=f"Unrelated decision {n}", body="filler")

    assert "msgpack" not in memory.opening_context()
    assert "msgpack" in memory.recall("msgpack serialisation").content


def test_recall_finds_a_prior_session_by_its_task(memory):
    memory.record_session(
        task="Migrate the auth module to tokens", status="max_steps",
        summary="Got halfway; the refresh path is unfinished.",
    )
    result = memory.recall("auth tokens")
    assert "refresh path is unfinished" in result.content


def test_recall_for_something_absent_says_so_plainly(memory):
    memory.record_session(task="t", status="finished", summary="s")
    result = memory.recall("kubernetes helm chart")
    assert result.ok
    assert result.data["hits"] == 0


def test_a_query_of_only_common_words_is_refused(memory):
    """It would match everything and rank nothing."""
    assert not memory.recall("what should I do with all of this").ok


def test_recall_output_is_bounded(memory):
    for n in range(50):
        record_decision(memory, title=f"Retry decision {n}", body="retry " * 2_000)
    # cap_text keeps half the budget from each end plus a marker saying what it
    # dropped, so the result lands just over the limit rather than exactly on it.
    assert len(memory.recall("retry").content) < skippy_memory.MAX_RECALL_CHARS + 200


# --- durability ---

def test_a_corrupt_meta_file_does_not_lose_the_history(memory, store, repo):
    memory.record_session(task="t", status="finished", summary="Worth keeping")
    record_decision(memory, title="Worth keeping too", body="reason")
    with open(memory.meta_path, "w", encoding="utf-8") as handle:
        handle.write("{ not json")

    reopened = open_project(root=store, workspace_roots=[str(repo)])
    assert "Worth keeping" in reopened.opening_context()
    assert len(reopened.decisions()) == 1


def test_a_corrupt_session_file_is_skipped_not_fatal(memory):
    memory.record_session(task="t", status="finished", summary="Good record")
    with open(os.path.join(memory.sessions_dir, "20200101-000000.json"), "w") as handle:
        handle.write("{ not json")
    assert "Good record" in memory.opening_context()


def test_a_stray_file_in_the_decisions_directory_is_ignored(memory):
    record_decision(memory, title="Real", body="reason")
    with open(os.path.join(memory.decisions_dir, "notes.txt"), "w") as handle:
        handle.write("not a decision")
    assert len(memory.decisions()) == 1


def test_listing_projects_reports_what_is_there(store, tmp_path):
    (tmp_path / "one").mkdir()
    first = open_project(root=store, workspace_roots=[str(tmp_path / "one")])
    first.record_session(task="t", status="finished", summary="s")
    (tmp_path / "two").mkdir()
    open_project(root=store, workspace_roots=[str(tmp_path / "two")])

    listed = {p["project_id"]: p for p in skippy_memory.list_projects(store)}
    assert len(listed) == 2
    assert listed[first.project_id]["sessions"] == 1


def test_listing_a_missing_store_is_empty_not_an_error(tmp_path):
    assert skippy_memory.list_projects(str(tmp_path / "nope")) == []


# --- work items: the RE-to-coding handoff ---
#
# A weakness is found while reading a built artifact and fixed by changing source, in
# two different sessions in two different modes. Nothing in the repository records
# that the weakness was ever noticed, so this is the only route from one to the other.

def raise_item(memory, title="Firmware update is unauthenticated", **kwargs):
    args = {
        "body": "The updater accepts any image with a valid CRC and checks no signature.",
        "severity": "critical",
        "confidence": "confirmed",
        "pack": "firmware-bin-1a2b3c4d",
        "finding": "0007",
        "target": "/opt/products/gate/firmware.bin",
    }
    args.update(kwargs)
    return memory.add_work_item(title=title, **args)


def test_a_weakness_reaches_the_next_session_without_being_asked_for(memory):
    """The whole point. A tool the model may call is a tool it mostly will not, so the
    item goes in the opening message rather than waiting to be searched for."""
    raise_item(memory)
    context = memory.opening_context()
    assert "Firmware update is unauthenticated" in context
    assert "critical" in context


def test_the_opening_context_names_the_pack_and_finding(memory):
    """So the coding session can read the evidence rather than trusting the summary."""
    raise_item(memory)
    context = memory.opening_context()
    assert "firmware-bin-1a2b3c4d" in context
    assert "0007" in context


def test_severity_and_confidence_both_travel(memory):
    """Severity alone would let a speculative critical arrive looking like a confirmed
    one, which is the same failure the confidence field exists to prevent."""
    raise_item(memory, title="Possible overflow in the parser",
               severity="critical", confidence="speculative")
    context = memory.opening_context()
    assert "critical" in context and "speculative" in context


def test_work_items_are_ordered_worst_first(memory):
    raise_item(memory, title="Verbose logging", severity="low")
    raise_item(memory, title="No signature check", severity="critical")
    raise_item(memory, title="Weak session token", severity="medium")
    titles = [item["front"]["title"] for item in memory.work_items()]
    assert titles == ["No signature check", "Weak session token", "Verbose logging"]


def test_an_item_with_no_severity_sorts_below_a_low_one(memory):
    """"Nobody said" and "somebody said this is minor" are different claims."""
    raise_item(memory, title="Unrated", severity="")
    raise_item(memory, title="Rated low", severity="low")
    titles = [item["front"]["title"] for item in memory.work_items()]
    assert titles == ["Rated low", "Unrated"]


def test_a_resolved_item_stops_arriving(memory):
    item = raise_item(memory)
    assert "unauthenticated" in memory.opening_context()

    result = skippy_memory.resolve_work_item(
        memory, item_id=item["id"], how="Added Ed25519 signature verification in updater.c."
    )
    assert result.ok
    assert "unauthenticated" not in memory.opening_context()


def test_resolving_never_modifies_the_original_record(memory):
    """Same append-only rule as superseding a finding or a decision: what was recorded
    at the time stays recorded, and how it was fixed is worth as much later."""
    item = raise_item(memory)
    before = open(item["path"], encoding="utf-8").read()
    skippy_memory.resolve_work_item(memory, item_id=item["id"], how="Signed the images.")
    assert open(item["path"], encoding="utf-8").read() == before


def test_how_it_was_fixed_survives_in_recall(memory):
    item = raise_item(memory)
    skippy_memory.resolve_work_item(
        memory, item_id=item["id"], how="Added Ed25519 verification in updater.c."
    )
    found = memory.recall("Ed25519")
    assert found.ok
    assert "updater.c" in found.content


def test_a_resolved_item_is_still_findable_and_marked(memory):
    """Hiding it would mean the next session investigates it again; showing it unmarked
    would mean fixing it again."""
    item = raise_item(memory)
    skippy_memory.resolve_work_item(memory, item_id=item["id"], how="Signed the images.")
    found = memory.recall("unauthenticated")
    assert "RESOLVED" in found.content


def test_resolving_needs_to_say_how(memory):
    item = raise_item(memory)
    result = skippy_memory.resolve_work_item(memory, item_id=item["id"], how="")
    assert not result.ok
    assert "how" in result.summary


def test_resolving_an_unknown_item_lists_the_open_ones(memory):
    """A refusal the model cannot act on costs real budget; naming the ids is what
    makes this correctable on the next step."""
    raise_item(memory)
    result = skippy_memory.resolve_work_item(memory, item_id="0042", how="Fixed it.")
    assert not result.ok
    assert "0001" in result.summary


def test_an_unpadded_item_id_still_resolves(memory):
    """The model will write 1, not 0001."""
    raise_item(memory)
    assert skippy_memory.resolve_work_item(memory, item_id="1", how="Fixed it.").ok


def test_resolving_the_same_item_twice_is_refused(memory):
    item = raise_item(memory)
    skippy_memory.resolve_work_item(memory, item_id=item["id"], how="Fixed it.")
    assert not skippy_memory.resolve_work_item(memory, item_id=item["id"], how="Again.").ok


def test_a_work_item_is_a_plain_file_a_person_can_read(memory):
    item = raise_item(memory)
    text = open(item["path"], encoding="utf-8").read()
    assert item["path"].endswith(".md")
    assert "# Firmware update is unauthenticated" in text
    assert "checks no signature" in text
    # Points at the record rather than standing in for it.
    assert "firmware-bin-1a2b3c4d" in text


def test_a_project_with_only_work_items_still_opens_with_context(memory):
    """The handoff has to work for a repo whose first session is the RE one."""
    raise_item(memory)
    assert memory.opening_context().strip()


def test_a_long_queue_is_truncated_and_says_so(memory):
    for index in range(skippy_memory.CONTEXT_WORK_ITEMS + 4):
        raise_item(memory, title=f"Weakness {index}", severity="medium")
    context = memory.opening_context()
    assert "and 4 more" in context


def test_a_stray_file_in_the_work_items_directory_is_ignored(memory):
    raise_item(memory)
    with open(os.path.join(memory.work_items_dir, "scratch.txt"), "w") as handle:
        handle.write("not a work item")
    assert len(memory.work_items()) == 1
