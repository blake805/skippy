"""Project memory: what a new session needs to know about work already done.

Implements the session and decision schema accepted in ADR 0003, which was designed
and never built in this lineage.

Without this, every session starts blind. It re-greps to find where things are,
re-derives the same architecture, and — the expensive one — re-explores dead ends
that were already ruled out, because a failed approach leaves no trace anywhere. The
agent gets no better at working on a repo no matter how many times it does.

Two things in here are not in ADR 0003, and both come from the same mistake made
twice already in this project.

**Recall is not optional.** The memory is assembled and put in the opening message,
not exposed only as a tool for the model to call when it feels the need. A tool the
model may call is a tool it mostly will not: RE mode had to announce its note pack up
front for the same reason, and even then the model batched its findings at the end
against explicit instructions. Anything that must happen has to be done by the loop.

**Memory says when it has gone stale.** This is the difference between memory that
helps and memory that hurts. "The retry logic lives in client.py" is confidently
wrong after that file is split, and the model believes it, because it arrives labelled
as established project knowledge rather than as a guess. So every entry records the
commit it was written at and the paths it refers to; on the way back out, paths that
no longer exist are marked. Exactly the same reasoning as marking a superseded finding
in `skippy_re` — an unmarked retracted conclusion is worse than a missing one.

Storage is plain files under `sessions_root()`, for the reasons ADR 0003 and ADR 0012
both give: they survive an unmounted NAS, they can be read by a person with no Skippy
running, and they diff. Vector search over them is a layer that can be added; making
it the storage would mean an unavailable backend loses the work. Recall here is
deterministic keyword scoring, which is also what makes it testable in CI.

**Work items are how a weakness found in RE mode reaches the code.** A finding in a
note pack exists to drive a fix in our own source, and those are two different
sessions in two different modes with nothing joining them. Both modes already open the
same project memory, keyed by the same workspace roots, so the join needs no new
keyspace: the RE loop raises a work item as the weakness is recorded, and a later
coding session opens with it already in front of it. Find it in RE mode, fix it in
coding mode.
"""

import json
import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Sequence

import skippy_paths
# For the severity vocabulary only. Severities belong with the findings that carry
# them, and restating the ordered list here would be a second source of truth of
# exactly the kind that goes stale quietly.
import skippy_re
from skippy_sandbox import ToolResult, cap_text

logger = logging.getLogger("skippy_memory")

SCHEMA_VERSION = 3

MAX_CONTEXT_CHARS = 6_000
MAX_DECISION_BODY_CHARS = 4_000
MAX_RECALL_CHARS = 12_000
MAX_WORK_ITEM_BODY_CHARS = 4_000
# How many past sessions to consider. Older ones stay on disk and are still
# greppable by hand; they are just not worth the context they would cost.
RECENT_SESSIONS = 8
CONTEXT_DECISIONS = 6
# Open work items carried into the opening block, worst first. This is a queue, and
# the top of a queue is the part that should change what the session does.
CONTEXT_WORK_ITEMS = 6

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_WORD = re.compile(r"[a-z0-9_./-]{3,}")

# Words that match everything and therefore rank nothing.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "was", "were",
    "add", "fix", "use", "using", "make", "made", "does", "did", "not", "but", "you",
    "what", "when", "where", "which", "why", "how", "all", "any", "can", "will",
    "should", "would", "could", "into", "out", "off", "than", "then", "there",
})


class MemoryError_(Exception):
    """The project store could not be read or written."""


def slugify(text: str, fallback: str = "project") -> str:
    slug = _SLUG_STRIP.sub("-", str(text or "").strip().lower()).strip("-")
    return (slug or fallback)[:60]


def project_id_for(roots: Sequence[str]) -> str:
    """A stable id for a set of workspace roots.

    Derived from the roots rather than supplied by a caller, so that opening the same
    repos tomorrow lands on the same memory without anyone having to remember a name.
    Sorted, because the order roots are listed in is not meaningful and must not
    produce a second project for the same set.
    """
    names = sorted(os.path.basename(str(root).rstrip(os.sep)) for root in roots if root)
    if not names:
        return "unscoped"
    if len(names) == 1:
        return slugify(names[0])
    # Several roots is one project — cross-repo work is the reason for having them —
    # so the id names all of them rather than picking a winner.
    return slugify("-".join(names))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def head_commit(path: str) -> str:
    """The commit a memory entry was written at, or "" outside a repo.

    Recorded so that a reader can tell how much has happened since. Cheap, and the
    only thing that makes "this may be out of date" answerable rather than a guess.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


class ProjectMemory:
    """One project's accumulated knowledge: sessions, decisions, conventions."""

    def __init__(self, root: str, project_id: str, workspace_roots: Optional[Sequence[str]] = None):
        self.project_id = project_id
        self.dir = os.path.join(root, project_id)
        self.sessions_dir = os.path.join(self.dir, "sessions")
        self.decisions_dir = os.path.join(self.dir, "decisions")
        self.work_items_dir = os.path.join(self.dir, "work_items")
        self.meta_path = os.path.join(self.dir, "meta.json")
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.decisions_dir, exist_ok=True)
        os.makedirs(self.work_items_dir, exist_ok=True)

        self.roots = [str(r) for r in (workspace_roots or [])]
        self.meta = _read_json(self.meta_path, None) or {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "created": _now(),
            "workspace_roots": [],
            "conventions": {},
        }
        changed = False
        for root_path in self.roots:
            if root_path not in self.meta["workspace_roots"]:
                self.meta["workspace_roots"].append(root_path)
                changed = True
        # Written only on a real change, so that reading a project does not touch its
        # timestamps and make every project look equally recent.
        if changed or not os.path.isfile(self.meta_path):
            self._save_meta()

    def _save_meta(self) -> None:
        self.meta["updated"] = _now()
        self.meta["schema_version"] = SCHEMA_VERSION
        _write_json(self.meta_path, self.meta)

    @property
    def primary_root(self) -> str:
        roots = self.roots or self.meta.get("workspace_roots") or []
        return roots[0] if roots else ""

    # -- conventions ------------------------------------------------------

    def learn_convention(self, key: str, value: str) -> None:
        """How this project does something — its test command, its formatter.

        Kept separate from decisions because it is a fact to be reused verbatim next
        time, not a judgment with reasoning behind it.
        """
        key, value = str(key).strip(), str(value).strip()
        if not key or not value:
            return
        if self.meta.setdefault("conventions", {}).get(key) != value:
            self.meta["conventions"][key] = value
            self._save_meta()

    # -- sessions ---------------------------------------------------------

    def record_session(
        self,
        task: str,
        status: str,
        summary: str,
        files_changed: Sequence[str] = (),
        findings: int = 0,
        steps: int = 0,
        mode: str = "coding",
    ) -> str:
        """Write the record of one run. Called by the loop on every terminal outcome.

        Including the ones that failed. A run that ran out of steps halfway through a
        migration is the single most useful thing for the next session to know, and it
        is exactly what a "save on success" rule would throw away.
        """
        # The counter is always present and fixed-width so that sorting the filenames
        # is sorting by time. An id that only grew a suffix on collision broke that:
        # "-2" sorts before "." in ASCII, so the second run of a given second came back
        # ahead of the first, and `sessions()` — whose entire contract is newest-first —
        # returned them jumbled and dropped the wrong ones at the limit.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        counter = 1
        session_id = f"{stamp}-{counter:02d}"
        path = os.path.join(self.sessions_dir, f"{session_id}.json")
        while os.path.exists(path):
            counter += 1
            session_id = f"{stamp}-{counter:02d}"
            path = os.path.join(self.sessions_dir, f"{session_id}.json")

        record = {
            "session_id": session_id,
            "recorded": _now(),
            "commit": head_commit(self.primary_root),
            "task": str(task)[:2_000],
            "status": status,
            "summary": str(summary)[:4_000],
            "files_changed": [str(f) for f in files_changed],
            "findings": int(findings),
            "steps": int(steps),
            "mode": mode,
        }
        _write_json(path, record)
        return session_id

    def sessions(self, limit: int = RECENT_SESSIONS) -> List[dict]:
        try:
            names = sorted(
                (n for n in os.listdir(self.sessions_dir) if n.endswith(".json")),
                reverse=True,
            )
        except OSError:
            return []
        return [_read_json(os.path.join(self.sessions_dir, n), {}) for n in names[:limit]]

    # -- decisions --------------------------------------------------------

    def next_decision_id(self) -> str:
        highest = 0
        for name in self._decision_names():
            head = name.split("-", 1)[0]
            if head.isdigit():
                highest = max(highest, int(head))
        return f"{highest + 1:04d}"

    def _decision_names(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.decisions_dir) if n.endswith(".md"))
        except OSError:
            return []

    def add_decision(
        self,
        title: str,
        body: str,
        affects: Sequence[str] = (),
        supersedes: str = "",
    ) -> dict:
        decision_id = self.next_decision_id()
        path = os.path.join(self.decisions_dir, f"{decision_id}-{slugify(title, 'decision')}.md")

        front = {
            "id": decision_id,
            "title": title,
            "recorded": _now(),
            "commit": head_commit(self.primary_root),
        }
        if affects:
            front["affects"] = ", ".join(str(a) for a in affects)
        if supersedes:
            front["supersedes"] = supersedes

        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# {title}", "", cap_text(str(body).strip(), MAX_DECISION_BODY_CHARS), ""]

        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.replace(tmp, path)
        return {"id": decision_id, "path": path, "title": title}

    def decisions(self) -> List[dict]:
        found = []
        for name in self._decision_names():
            path = os.path.join(self.decisions_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                continue
            found.append({"path": path, "front": _parse_front(text), "text": text})
        return found

    def superseded_ids(self) -> set:
        """Derived from the newer decisions, so the older files are never rewritten."""
        ids = set()
        for item in self.decisions():
            target = item["front"].get("supersedes")
            if target:
                ids.update(p.strip() for p in str(target).split(",") if p.strip())
        return ids

    # -- work items -------------------------------------------------------

    def _work_item_names(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.work_items_dir) if n.endswith(".md"))
        except OSError:
            return []

    def _read_work_item_files(self) -> List[dict]:
        found = []
        for name in self._work_item_names():
            path = os.path.join(self.work_items_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                continue
            found.append({"path": path, "front": _parse_front(text), "text": text})
        return found

    def next_work_item_id(self) -> str:
        highest = 0
        for name in self._work_item_names():
            head = name.split("-", 1)[0]
            if head.isdigit():
                highest = max(highest, int(head))
        return f"{highest + 1:04d}"

    def add_work_item(
        self,
        title: str,
        body: str,
        severity: str = "",
        confidence: str = "",
        pack: str = "",
        finding: str = "",
        target: str = "",
    ) -> dict:
        """Raise one thing that needs fixing in our own code.

        Written by the RE loop as a weakness is recorded, not called by the model. The
        model's job was the judgment that something is wrong and how urgent it is; the
        handoff to the next session is plumbing, and plumbing left to the model is
        plumbing that mostly does not happen.

        `pack` and `finding` are carried so the coding session can read the evidence
        rather than trusting this summary of it, and `severity` travels with
        `confidence` because a speculative critical and a confirmed critical are
        different work.
        """
        item_id = self.next_work_item_id()
        path = os.path.join(self.work_items_dir, f"{item_id}-{slugify(title, 'work-item')}.md")

        front = {
            "id": item_id,
            "title": title,
            "recorded": _now(),
            "commit": head_commit(self.primary_root),
        }
        for key, value in (
            ("severity", severity), ("confidence", confidence),
            ("pack", pack), ("finding", finding), ("target", target),
        ):
            if value:
                front[key] = value

        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# {title}", "",
                  cap_text(str(body).strip(), MAX_WORK_ITEM_BODY_CHARS), ""]
        if pack and finding:
            lines += [
                "",
                f"Evidence is in note pack `{pack}`, finding {finding}. Read it before "
                "changing anything: this is a summary, and the finding is the record.",
                "",
            ]

        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.replace(tmp, path)
        return {"id": item_id, "path": path, "title": title, "severity": severity}

    def add_resolution(self, resolves: str, how: str) -> dict:
        """Mark a work item done, by writing a new record rather than editing it.

        Same shape as superseding a finding or a decision, and for the same reason:
        what was recorded at the time stays recorded, and how something was fixed is
        worth as much later as the fact that it was.
        """
        item_id = self.next_work_item_id()
        path = os.path.join(self.work_items_dir, f"{item_id}-resolved-{slugify(resolves, 'item')}.md")

        front = {
            "id": item_id,
            "title": f"Resolved work item {resolves}",
            "resolves": resolves,
            "recorded": _now(),
            "commit": head_commit(self.primary_root),
        }
        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# Resolved work item {resolves}", "",
                  cap_text(str(how).strip(), MAX_WORK_ITEM_BODY_CHARS), ""]

        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.replace(tmp, path)
        return {"id": item_id, "path": path, "resolves": resolves}

    def resolved_ids(self) -> set:
        """Derived from the resolution records, so no work item is ever rewritten."""
        ids = set()
        for item in self._read_work_item_files():
            target = item["front"].get("resolves")
            if target:
                ids.update(p.strip() for p in str(target).split(",") if p.strip())
        return ids

    def work_items(self) -> List[dict]:
        """The open work items, worst first.

        Resolutions live in the same directory because they are the same kind of
        append-only record, and are told apart by carrying `resolves`. Resolved items
        are excluded here and still reachable through `recall`, the same treatment a
        superseded decision gets.
        """
        resolved = self.resolved_ids()
        items = []
        for entry in self._read_work_item_files():
            front = entry["front"]
            if front.get("resolves") or front.get("id") in resolved:
                continue
            items.append(entry)
        items.sort(
            key=lambda e: (
                skippy_re.severity_rank(e["front"].get("severity")),
                e["front"].get("id", ""),
            ),
            reverse=True,
        )
        return items

    # -- staleness --------------------------------------------------------

    def stale_paths(self, entry: dict) -> List[str]:
        """Which paths this entry refers to that are no longer there.

        The whole point: a decision about a file that has since been deleted or
        renamed is not merely useless, it is misinformation delivered with the
        authority of project history. Marking it costs one stat per path.
        """
        raw = entry.get("front", {}).get("affects") if "front" in entry else entry.get("files_changed")
        if not raw:
            return []
        candidates = (
            [p.strip() for p in str(raw).split(",")] if isinstance(raw, str) else [str(p) for p in raw]
        )
        missing = []
        for candidate in candidates:
            if not candidate:
                continue
            if not any(
                os.path.exists(os.path.join(root, candidate)) or os.path.exists(candidate)
                for root in (self.roots or [self.primary_root])
                if root
            ):
                missing.append(candidate)
        return missing

    # -- what a new session opens with ------------------------------------

    def opening_context(self) -> str:
        """The block handed to a new run, bounded and marked where it may be wrong.

        Deliberately short. This competes for the same context the actual task needs,
        and a wall of history is how you get a model that pattern-matches to last
        week's work instead of reading this week's request.
        """
        sessions = self.sessions()
        decisions = self.decisions()
        work_items = self.work_items()
        if not sessions and not decisions and not work_items and not self.meta.get("conventions"):
            return ""

        blocks = [f"## What you already know about this project ({self.project_id})"]

        conventions = self.meta.get("conventions") or {}
        if conventions:
            blocks.append(
                "Conventions established here:\n"
                + "\n".join(f"- {key}: {value}" for key, value in sorted(conventions.items()))
            )

        # Ahead of the decisions and the session history, because this is the only part
        # of the block that is a request rather than background. A weakness found in an
        # RE session has no other route into the code: the artifact was not ours to
        # change, so nothing in the repository shows it was ever noticed.
        if work_items:
            lines = [
                "Open weaknesses found while reverse-engineering these products, worst "
                "first. Each names the note pack and finding holding the evidence — read "
                "that before changing code, because the line below is a summary:"
            ]
            for entry in work_items[:CONTEXT_WORK_ITEMS]:
                front = entry["front"]
                where = ""
                if front.get("pack") and front.get("finding"):
                    where = f" [pack {front['pack']}, finding {front['finding']}]"
                lines.append(
                    f"- [{front.get('id')}] {front.get('severity', '?')}, "
                    f"{front.get('confidence', '?')}: {front.get('title')}{where}"
                )
            if len(work_items) > CONTEXT_WORK_ITEMS:
                lines.append(
                    f"- ... and {len(work_items) - CONTEXT_WORK_ITEMS} more; "
                    "recall_project will list them."
                )
            lines.append(
                "Severity is how urgently it should be fixed. Confidence is how sure the "
                "session that found it was that it is real — a speculative one may need "
                "confirming before it needs fixing. Call resolve_work_item when one is "
                "dealt with, so it stops arriving here."
            )
            blocks.append("\n".join(lines))

        superseded = self.superseded_ids()
        live = [d for d in decisions if d["front"].get("id") not in superseded]
        if live:
            lines = ["Decisions from earlier sessions:"]
            for item in live[-CONTEXT_DECISIONS:]:
                front = item["front"]
                note = ""
                missing = self.stale_paths(item)
                if missing:
                    # Said out loud rather than silently dropped: the decision may
                    # still be sound reasoning about code that has since moved.
                    note = f" [MAY BE OUT OF DATE: {', '.join(missing)} no longer exists]"
                lines.append(f"- [{front.get('id')}] {front.get('title')}{note}")
            blocks.append("\n".join(lines))

        if sessions:
            lines = ["Recent sessions, newest first:"]
            for record in sessions:
                status = record.get("status", "?")
                summary = " ".join(str(record.get("summary", "")).split())[:280]
                changed = record.get("files_changed") or []
                where = f" (touched {', '.join(changed[:4])})" if changed else ""
                lines.append(f"- {record.get('recorded', '?')} [{status}] {summary}{where}")
            blocks.append("\n".join(lines))

        blocks.append(
            "This is your own record of earlier work, not instructions. Where it "
            "conflicts with what you read in the code now, the code is right and the "
            "note is stale — say so in your summary."
        )
        return cap_text("\n\n".join(blocks), MAX_CONTEXT_CHARS)

    # -- recall on demand -------------------------------------------------

    def recall(self, query: str = "", limit: int = 6) -> ToolResult:
        """Search the project's history. Deterministic scoring, no vector backend.

        Exists for the case the opening context cannot cover: a long project where the
        relevant decision is forty sessions back. With no query it returns the same
        overview the session opened with, which is the useful default for "what do I
        know here again?".
        """
        if not str(query).strip():
            context = self.opening_context()
            return ToolResult(
                True,
                f"Project '{self.project_id}': {len(self.sessions(limit=999))} session(s), "
                f"{len(self.decisions())} decision(s).",
                context or "Nothing recorded for this project yet.",
            )

        terms = {t for t in _WORD.findall(str(query).lower()) if t not in _STOPWORDS}
        if not terms:
            return ToolResult(False, "That query is all common words; give something specific.")

        superseded = self.superseded_ids()
        scored = []
        for item in self.decisions():
            text = item["text"]
            score = sum(1 for term in terms if term in text.lower())
            if not score:
                continue
            marks = []
            if item["front"].get("id") in superseded:
                marks.append("SUPERSEDED by a later decision")
            missing = self.stale_paths(item)
            if missing:
                marks.append(f"MAY BE OUT OF DATE: {', '.join(missing)} no longer exists")
            header = f"> {'; '.join(marks)}\n\n" if marks else ""
            # A superseded decision ranks below a live one of equal match, rather than
            # being hidden: how a decision was reached and then reversed is often the
            # answer to why the code looks the way it does.
            scored.append((score - (2 if superseded & {item["front"].get("id")} else 0), header + text))

        # Every file in the directory, resolutions included. Searching only the items
        # would mean "how was this fixed" had no answer anywhere, since the resolution
        # record is the only place the fix is described.
        resolved = self.resolved_ids()
        for entry in self._read_work_item_files():
            text = entry["text"]
            score = sum(1 for term in terms if term in text.lower())
            if not score:
                continue
            # A resolved item ranks below its own resolution: both match a query about
            # the weakness, and the useful one is the account of the fix. Neither is
            # hidden — "this was already found and dealt with" is the answer to a lot
            # of questions, and the alternative is fixing it twice.
            is_resolved = entry["front"].get("id") in resolved
            header = "> RESOLVED by a later record.\n\n" if is_resolved else ""
            scored.append((score - (2 if is_resolved else 0), header + text))

        for record in self.sessions(limit=40):
            blob = json.dumps(record).lower()
            score = sum(1 for term in terms if term in blob)
            if score:
                scored.append((score, _render_session(record)))

        if not scored:
            return ToolResult(
                True,
                f"Nothing in this project's memory matches '{query}'.",
                "",
                {"hits": 0},
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        body = "\n\n---\n\n".join(text for _, text in scored[:limit])
        return ToolResult(
            True,
            f"{min(len(scored), limit)} match(es) for '{query}' in project '{self.project_id}'.",
            cap_text(body, MAX_RECALL_CHARS),
            {"hits": len(scored)},
        )


def _render_session(record: dict) -> str:
    changed = record.get("files_changed") or []
    return (
        f"SESSION {record.get('session_id')} [{record.get('status')}] "
        f"at commit {record.get('commit') or 'unknown'}\n"
        f"task: {record.get('task')}\n"
        f"outcome: {record.get('summary')}\n"
        f"files: {', '.join(changed) or 'none'}"
    )


def _yaml_scalar(value: str) -> str:
    text = str(value).replace("\n", " ").strip()
    if any(ch in text for ch in ':#"\'[]{}') or not text:
        return json.dumps(text)
    return text


def _parse_front(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    front = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        if raw.startswith('"'):
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        front[key.strip()] = raw
    return front


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def record_decision(
    memory: ProjectMemory,
    title: str = "",
    body: str = "",
    affects: str = "",
    supersedes: str = "",
) -> ToolResult:
    """Record a choice that a later session would otherwise have to re-litigate.

    Model-called rather than extracted automatically, because a decision is a judgment
    with reasoning behind it, and an extractor turns those into bland restatements of
    the diff. The session record is auto-written; this is for the part only the model
    knows.
    """
    title = str(title or "").strip()
    body = str(body or "").strip()
    if not title:
        return ToolResult(False, "record_decision needs a 'title'.")
    if not body:
        return ToolResult(
            False,
            "record_decision needs a 'body' giving the reasoning. A title alone tells a "
            "later session what was chosen but not why, which is the part that stops it "
            "being undone by accident.",
        )

    if supersedes:
        known = {item["front"].get("id") for item in memory.decisions()}
        missing = [
            p.strip() for p in str(supersedes).split(",")
            if p.strip() and p.strip() not in known
        ]
        if missing:
            return ToolResult(
                False,
                f"Cannot supersede decision(s) {', '.join(missing)}: no such id in this "
                f"project. Known ids: {', '.join(sorted(i for i in known if i)) or '(none)'}.",
            )

    affected = [p.strip() for p in str(affects or "").split(",") if p.strip()]
    try:
        record = memory.add_decision(title, body, affects=affected, supersedes=supersedes)
    except OSError as exc:
        return ToolResult(False, f"Could not write the decision: {exc}")

    note = f" (supersedes {supersedes})" if supersedes else ""
    return ToolResult(
        True,
        f"Recorded decision {record['id']}: {title}{note}",
        "",
        {"decision": record, "project": memory.project_id},
    )


def recall_project(memory: ProjectMemory, query: str = "") -> ToolResult:
    """Search earlier sessions and decisions for this project."""
    return memory.recall(query)


def resolve_work_item(memory: ProjectMemory, item_id: str = "", how: str = "") -> ToolResult:
    """Close out a weakness raised by an earlier reverse-engineering session.

    Model-called, because whether a fix actually addresses the weakness is a judgment
    and nothing the loop can observe. Raising the item is plumbing and is done by the
    loop; deciding it is dealt with is not.

    Refuses an unknown id by listing the open ones, so a model that guessed a number
    can correct on the next step rather than retrying — a refusal the model cannot act
    on costs real budget.
    """
    item_id = str(item_id or "").strip().lstrip("#")
    how = str(how or "").strip()
    if not item_id:
        return ToolResult(False, "resolve_work_item needs the 'item_id' of the work item.")
    if not how:
        return ToolResult(
            False,
            "resolve_work_item needs 'how': what you changed that addresses it. Without "
            "that, a later session sees the item closed and has no way to tell whether "
            "the weakness was fixed, mitigated elsewhere, or judged not to apply.",
        )

    open_items = memory.work_items()
    known = {entry["front"].get("id") for entry in open_items}
    if item_id not in known and item_id.zfill(4) in known:
        item_id = item_id.zfill(4)
    if item_id not in known:
        return ToolResult(
            False,
            f"No open work item '{item_id}' in this project. Open ids: "
            f"{', '.join(sorted(i for i in known if i)) or '(none)'}.",
        )

    try:
        record = memory.add_resolution(item_id, how)
    except OSError as exc:
        return ToolResult(False, f"Could not record the resolution: {exc}")

    return ToolResult(
        True,
        f"Work item {item_id} marked resolved. It will not appear in later sessions.",
        "",
        {"resolution": record, "project": memory.project_id},
    )


def open_project(
    root: Optional[str] = None,
    workspace_roots: Optional[Sequence[str]] = None,
    project_id: str = "",
) -> ProjectMemory:
    """Open or create the memory for a set of workspace roots.

    Never model-controlled, for the same reason as the sandbox and the note pack: a
    model that could choose the project could read another one's history or scatter
    this one's across several.
    """
    base = root or os.path.join(skippy_paths.sessions_root(), "projects")
    os.makedirs(base, exist_ok=True)
    chosen = project_id or project_id_for(workspace_roots or [])
    if os.sep in chosen or chosen in ("", ".", ".."):
        raise MemoryError_(f"Unsafe project id: {chosen!r}")
    return ProjectMemory(base, chosen, workspace_roots=workspace_roots)


def list_projects(root: Optional[str] = None) -> List[Dict[str, object]]:
    base = root or os.path.join(skippy_paths.sessions_root(), "projects")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    projects = []
    for name in names:
        meta = _read_json(os.path.join(base, name, "meta.json"), None)
        if meta is None:
            continue
        sessions_dir = os.path.join(base, name, "sessions")
        try:
            count = len([n for n in os.listdir(sessions_dir) if n.endswith(".json")])
        except OSError:
            count = 0
        projects.append({
            "project_id": name,
            "workspace_roots": meta.get("workspace_roots", []),
            "sessions": count,
            "updated": meta.get("updated", ""),
        })
    return projects
