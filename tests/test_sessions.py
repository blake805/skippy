"""Project memory: schema on disk, and continuity between separate sessions."""

import json
import os

import pytest

import skippy_sessions
from skippy_sessions import SessionStore, slugify


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=str(tmp_path / "projects"))


def test_slugify_makes_collection_safe_names():
    assert slugify("shop-jarvis") == "shop_jarvis"
    assert slugify("My Project!! 2") == "my_project_2"
    assert slugify("") == "project"


def test_ensure_project_writes_the_documented_schema(store, sample_repo):
    meta = store.ensure_project(
        "shop-jarvis",
        name="Shop Jarvis",
        workspace_roots=[sample_repo],
        conventions={"test_command": "pytest -q"},
    )

    on_disk = json.loads(open(store.meta_path("shop-jarvis"), encoding="utf-8").read())
    assert on_disk == meta
    assert meta["project_id"] == "shop-jarvis"
    assert meta["workspace_roots"] == [sample_repo]
    assert meta["conventions"]["test_command"] == "pytest -q"
    assert meta["chroma_collections"] == {
        "code": "proj_shop_jarvis_code",
        "notes": "proj_shop_jarvis_notes",
    }
    assert meta["stats"] == {"sessions": 0, "files_touched": 0}
    assert store.list_projects() == ["shop_jarvis"]


def test_ensure_project_is_idempotent_and_merges_roots(store, sample_repo, tmp_path):
    store.ensure_project("p", workspace_roots=[sample_repo])
    second = tmp_path / "other"
    second.mkdir()
    meta = store.ensure_project("p", workspace_roots=[sample_repo, str(second)])

    assert meta["workspace_roots"] == [sample_repo, str(second)]
    assert meta["created_at"] <= meta["updated_at"]


def test_git_remote_is_recorded_for_repo_roots(store, sample_git_repo):
    meta = store.ensure_project("p", workspace_roots=[sample_git_repo])
    assert len(meta["repos"]) == 1
    assert meta["repos"][0]["path"] == sample_git_repo
    assert meta["repos"][0]["default_branch"]


def test_session_records_turns_and_finishes(store, sample_repo):
    session = store.start_session(
        project_id="p", session_id="s-1", task="do a thing", mode="Agent",
        workspace_roots=[sample_repo],
    )
    session.record_turn(step=1, tool="read_file", args={"path": "calc/ops.py"}, ok=True,
                        result_summary="read it", thought="looking")
    session.record_turn(step=2, tool="apply_patch", args={"edits": []}, ok=True,
                        result_summary="patched")
    session.finish(status="success", summary="Did the thing.", files_changed=["calc/ops.py"])

    saved = store.load_session("p", "s-1")
    assert saved["schema_version"] == skippy_sessions.SCHEMA_VERSION
    assert saved["status"] == "success"
    assert saved["summary"] == "Did the thing."
    assert saved["files_touched"] == ["calc/ops.py"]
    assert saved["ended_at"]
    assert [turn["tool"] for turn in saved["turns"]] == ["read_file", "apply_patch"]
    assert saved["turns"][0]["step"] == 1
    assert set(saved["model"]) == {"fast", "heavy", "compressor"}

    meta = store.project_meta("p")
    assert meta["stats"]["sessions"] == 1
    assert meta["all_files_touched"] == ["calc/ops.py"]
    assert meta["last_session"]["session_id"] == "s-1"


def test_bulky_turn_arguments_are_truncated_on_disk(store, sample_repo):
    session = store.start_session("p", "s-1", "t", "Agent", [sample_repo])
    session.record_turn(step=1, tool="apply_patch", args={"blob": "x" * 50_000})

    saved = store.load_session("p", "s-1")
    stored = saved["turns"][0]["args"]["blob"]
    assert len(stored) < skippy_sessions.MAX_TURN_ARG_CHARS + 100
    assert stored.endswith("chars]")


def test_resuming_a_session_id_keeps_prior_turns(store, sample_repo):
    first = store.start_session("p", "s-1", "t", "Agent", [sample_repo])
    first.record_turn(step=1, tool="read_file")
    first.finish("success", "part one", ["a.py"])

    resumed = store.start_session("p", "s-1", "t (continued)", "Agent", [sample_repo])
    assert len(resumed.turns) == 1
    assert resumed.files_touched == ["a.py"]
    resumed.record_turn(step=2, tool="apply_patch")
    resumed.finish("success", "part two", ["b.py"])

    saved = store.load_session("p", "s-1")
    assert len(saved["turns"]) == 2
    assert saved["files_touched"] == ["a.py", "b.py"]


def test_backup_dir_is_per_session_and_step(store, sample_repo):
    session = store.start_session("p", "s-1", "t", "Agent", [sample_repo])
    path = session.backup_dir(7)
    assert path.endswith(os.path.join("patches", "s-1", "7"))
    # Not created eagerly; apply_patch makes it only when there is a pre-image.
    assert not os.path.exists(path)


async def test_decisions_are_readable_markdown_with_front_matter(store):
    memory = store.memory_for("p")
    store.ensure_project("p")

    result = await memory.save_decision(
        title="Use search/replace patches",
        body="Line numbers are unreliable from local models.",
        tags=["architecture", "patching"],
        session_id="s-1",
    )

    assert result.ok
    assert result.data["decision_id"] == "dec-0001"
    path = os.path.join(store.decisions_dir("p"), "dec-0001.md")
    text = open(path, encoding="utf-8").read()
    assert text.startswith("---\n")
    assert 'id: dec-0001' in text
    assert '"architecture", "patching"' in text
    assert 'session_id: "s-1"' in text
    assert "# Use search/replace patches" in text
    assert "Line numbers are unreliable" in text


async def test_decision_ids_increment(store):
    memory = store.memory_for("p")
    store.ensure_project("p")
    first = await memory.save_decision("one", "body")
    second = await memory.save_decision("two", "body")
    assert (first.data["decision_id"], second.data["decision_id"]) == ("dec-0001", "dec-0002")


async def test_a_later_session_recalls_earlier_decisions(store, sample_repo):
    """The Phase 3 exit criterion, without a vector backend."""
    memory = store.memory_for("p")
    session = store.start_session("p", "s-1", "Wire up the retry logic", "Agent", [sample_repo])
    await memory.save_decision(
        title="Retries use exponential backoff",
        body="query_model retries three times with 2/4/8 second sleeps.",
        tags=["networking"],
        session_id="s-1",
    )
    session.record_turn(step=1, tool="apply_patch", result_summary="patched skippy_llm.py")
    session.finish("success", "Added retry with backoff.", ["skippy_llm.py"])

    recall = await memory.search("how do retries and backoff work", k=5)

    assert recall.ok
    assert recall.data["hits"] >= 1
    assert "exponential backoff" in recall.content
    assert "skippy_llm.py" in recall.content


async def test_search_is_scoped_to_one_project(store, sample_repo):
    for project in ("alpha", "beta"):
        store.ensure_project(project)
        await store.memory_for(project).save_decision(
            f"{project} decision", f"secret detail for {project} about caching", tags=[project]
        )

    alpha = await store.memory_for("alpha").search("caching detail")
    beta = await store.memory_for("beta").search("caching detail")

    assert "secret detail for alpha" in alpha.content
    assert "secret detail for beta" not in alpha.content
    assert "secret detail for beta" in beta.content


async def test_empty_memory_reports_cleanly(store):
    result = await store.memory_for("fresh").search("anything")
    assert result.ok
    assert result.data["hits"] == 0


async def test_oversized_recall_is_compressed(routed_llm, store, sample_repo):
    routed_llm.compressor_reply = "Everything is about caching."
    memory = store.memory_for("p")
    store.ensure_project("p")
    for index in range(12):
        await memory.save_decision(
            f"caching decision {index}", "caching " + ("detail " * 400), tags=["caching"]
        )

    recall = await memory.search("caching", k=12)

    assert recall.content == "Everything is about caching."


# ---------------------------------------------------------------------------
# Vector-backed path, exercised with a stub client
# ---------------------------------------------------------------------------

class StubCollection:
    def __init__(self, name):
        self.name = name
        self.documents = {}

    def upsert(self, documents, metadatas, ids):
        for document, metadata, doc_id in zip(documents, metadatas, ids):
            self.documents[doc_id] = (document, metadata)

    def query(self, query_texts, n_results):
        terms = set(query_texts[0].lower().split())
        ranked = sorted(
            self.documents.values(),
            key=lambda pair: -sum(1 for term in terms if term in pair[0].lower()),
        )[:n_results]
        return {
            "documents": [[document for document, _ in ranked]],
            "metadatas": [[metadata for _, metadata in ranked]],
        }


class StubChroma:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name):
        return self.collections.setdefault(name, StubCollection(name))


@pytest.fixture
def vector_store(tmp_path):
    chroma = StubChroma()
    store = SessionStore(root=str(tmp_path / "projects"), chroma_client_factory=lambda: chroma)
    return store, chroma


async def test_notes_land_in_the_project_scoped_collection(vector_store):
    store, chroma = vector_store
    store.ensure_project("shop-jarvis")
    await store.memory_for("shop-jarvis").save_decision("Scoped memory", "one bag per project")

    assert "proj_shop_jarvis_notes" in chroma.collections
    stored = chroma.collections["proj_shop_jarvis_notes"].documents
    assert any("one bag per project" in document for document, _ in stored.values())


async def test_finished_sessions_are_indexed_as_notes(vector_store, sample_repo):
    store, chroma = vector_store
    session = store.start_session("p", "s-9", "add divide", "Agent", [sample_repo])
    session.record_turn(step=1, tool="apply_patch", result_summary="ok")
    session.finish("success", "Added divide().", ["calc/ops.py"])

    stored = chroma.collections["proj_p_notes"].documents
    assert "p:s-9" in stored
    document = stored["p:s-9"][0]
    assert "add divide" in document
    assert "calc/ops.py" in document
    assert "apply_patch" in document


async def test_vector_search_blends_notes_and_code(vector_store):
    store, chroma = vector_store
    store.ensure_project("p")
    await store.memory_for("p").save_decision("Use backoff", "retry with exponential backoff")
    code = chroma.get_or_create_collection("proj_p_code")
    code.upsert(
        documents=["skippy_llm.py (lines 1-20):\nasync def query_model(): backoff here"],
        metadatas=[{"relative_path": "skippy_llm.py", "source": "skippy_llm.py"}],
        ids=["code:p:skippy_llm.py:0"],
    )

    recall = await store.memory_for("p").search("backoff", k=4)

    assert "PRIOR DECISIONS AND SESSIONS:" in recall.content
    assert "INDEXED CODE:" in recall.content
    assert "exponential backoff" in recall.content
    assert "query_model" in recall.content


async def test_index_workspace_upserts_path_aware_chunks(vector_store, sample_repo):
    store, chroma = vector_store
    store.ensure_project("p", workspace_roots=[sample_repo])

    first = await store.index_workspace("p", sample_repo)
    assert first.ok
    documents = chroma.collections["proj_p_code"].documents
    assert documents
    assert any(doc_id.startswith("code:p:calc/ops.py") for doc_id in documents)

    sample_id = next(doc_id for doc_id in documents if "calc/ops.py" in doc_id)
    _, metadata = documents[sample_id]
    assert metadata["relative_path"] == "calc/ops.py"
    assert metadata["project_id"] == "p"
    assert metadata["start_line"] == 1
    assert metadata["end_line"] >= 1

    # Re-ingesting replaces chunks rather than duplicating them.
    count_before = len(documents)
    await store.index_workspace("p", sample_repo)
    assert len(chroma.collections["proj_p_code"].documents) == count_before


def test_missing_vector_backend_degrades_instead_of_raising(tmp_path):
    def explode():
        raise RuntimeError("chroma unavailable")

    store = SessionStore(root=str(tmp_path / "projects"), chroma_client_factory=explode)
    assert store.collection("p", "notes") is None
    assert store.query_notes("p", "anything", 3) == []
