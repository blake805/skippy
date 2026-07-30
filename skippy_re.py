"""Reverse-engineering note packs.

In a coding task the product is a diff, and the repository remembers it. In a
reverse-engineering task nothing is written to the target at all, so unless the
findings are recorded somewhere they exist only in a transcript that will be folded,
truncated, or thrown away at the end of the session. The notes *are* the deliverable.

That shapes every decision here.

**Findings are files, not rows in a vector store.** One markdown file per finding,
under `notes_root()`. They survive an unmounted NAS, diff cleanly, can be grepped,
and can be read six months later by a person with no Skippy running. Search is a
layer that can be added over files; if search were the storage, an unavailable
vector store would mean losing the work.

**Evidence is mandatory.** `note_finding` refuses a finding with no evidence. This
is the single most valuable constraint in the module: "the header is 32 bytes" is
worthless six months on, while "the header is 32 bytes; the first section offset in
the load command at +0x18 is 0x20" can be rechecked. A model that cannot say where
it saw something usually inferred it, and needs to say so.

**Confidence is mandatory, and separate from evidence.** RE is mostly inference, and
the failure mode is a plausible guess hardening into an assumed fact as it gets cited
by later reasoning. Recording `confirmed` / `likely` / `speculative` at the point of
writing is what keeps a chain of inference auditable.

**Findings are append-only, and corrections supersede rather than overwrite.** Being
wrong and then right is the normal shape of reverse engineering, and the fact that a
conclusion changed — and why — is itself a finding. `supersedes` records that,
without mutating the earlier file. Same reasoning as the append-only transcript in
`skippy_llm`.

**Open questions are a kind of finding.** Most of an RE session is things not yet
understood, and an unrecorded unknown gets rediscovered from scratch next session.
"""

import json
import os
import re
import time
from typing import Dict, List, Optional

from skippy_sandbox import ToolResult, cap_text

MAX_NOTES_CHARS = 24_000
MAX_BODY_CHARS = 8_000
MAX_EVIDENCE_CHARS = 4_000

# A small fixed taxonomy on purpose. Left free, a model invents a new kind for
# nearly every finding and the pack stops being navigable; these cover what RE
# actually produces.
KINDS = {
    "structure",   # layout: headers, records, offsets, field meanings
    "behavior",    # what a routine or component does
    "constant",    # magic numbers, keys, tables, sentinel values
    "symbol",      # names, mangling, imports, exports, entry points
    "hypothesis",  # a theory not yet tested, with a way to test it
    "question",    # something not yet understood, recorded so it is not lost
}

# Ordered weakest-first; the ordering is used when reporting a pack's overall
# standing, where the weakest link is what matters.
CONFIDENCE = ("speculative", "likely", "confirmed")

# Only an open question may be recorded with nothing behind it. A hypothesis is
# deliberately not on this list: its whole value is being testable later, which
# requires saying what prompted it.
EVIDENCE_OPTIONAL = frozenset({"question"})

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class NotesError(Exception):
    """The note pack could not be read or written."""


def slugify(text: str, fallback: str = "target") -> str:
    slug = _SLUG_STRIP.sub("-", str(text or "").strip().lower()).strip("-")
    return (slug or fallback)[:60]


class NotePack:
    """One investigation: a target, and everything established about it.

    Keyed by target rather than by session, so a second session on the same artifact
    accumulates onto the first instead of starting an unrelated pack. Rediscovering
    last week's conclusions is the most common waste in reverse engineering.
    """

    def __init__(self, root: str, pack_id: str, target: str = "", title: str = ""):
        self.pack_id = pack_id
        self.dir = os.path.join(root, pack_id)
        self.findings_dir = os.path.join(self.dir, "findings")
        self.meta_path = os.path.join(self.dir, "pack.json")
        os.makedirs(self.findings_dir, exist_ok=True)

        self.meta = self._load_meta()
        # Only fill these in, never overwrite: reopening a pack must not silently
        # relabel an investigation that already has findings in it.
        changed = False
        if target and not self.meta.get("target"):
            self.meta["target"] = target
            changed = True
        if title and not self.meta.get("title"):
            self.meta["title"] = title
            changed = True
        # Written only when something actually changed, so that merely opening a pack
        # to read it does not move `updated`. That field is how you find the live
        # investigation among a directory of them, which it cannot do if looking
        # counts as touching.
        if changed or not os.path.isfile(self.meta_path):
            self._save_meta()

    # -- metadata ---------------------------------------------------------

    def _load_meta(self) -> dict:
        if os.path.isfile(self.meta_path):
            try:
                with open(self.meta_path, encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                # A corrupt pack.json must not lose the findings, which are the
                # valuable part and are stored separately.
                pass
        return {"pack_id": self.pack_id, "created": _now(), "target": "", "title": ""}

    def _save_meta(self) -> None:
        self.meta["updated"] = _now()
        self.meta["findings"] = len(self.finding_files())
        tmp = f"{self.meta_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.meta, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.meta_path)

    # -- findings ---------------------------------------------------------

    def finding_files(self) -> List[str]:
        try:
            names = sorted(
                name for name in os.listdir(self.findings_dir) if name.endswith(".md")
            )
        except OSError:
            return []
        return [os.path.join(self.findings_dir, name) for name in names]

    def next_id(self) -> str:
        highest = 0
        for path in self.finding_files():
            head = os.path.basename(path).split("-", 1)[0]
            if head.isdigit():
                highest = max(highest, int(head))
        return f"{highest + 1:04d}"

    def add(
        self,
        kind: str,
        title: str,
        body: str,
        evidence: str,
        confidence: str,
        location: str = "",
        supersedes: str = "",
    ) -> dict:
        finding_id = self.next_id()
        path = os.path.join(self.findings_dir, f"{finding_id}-{slugify(title, 'finding')}.md")

        front = {
            "id": finding_id,
            "kind": kind,
            "title": title,
            "confidence": confidence,
            "recorded": _now(),
        }
        if location:
            front["location"] = location
        if supersedes:
            front["supersedes"] = supersedes

        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# {title}", "", cap_text(body.strip(), MAX_BODY_CHARS), ""]
        lines += ["## Evidence", "", cap_text(evidence.strip(), MAX_EVIDENCE_CHARS), ""]

        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.replace(tmp, path)

        self._save_meta()
        self.write_index()
        return {"id": finding_id, "path": path, "kind": kind, "confidence": confidence}

    def read_findings(self) -> List[dict]:
        found = []
        for path in self.finding_files():
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                continue
            found.append({"path": path, "front": _parse_front(text), "text": text})
        return found

    def superseded_ids(self) -> set:
        """Derived from the newer findings rather than stored on the older ones.

        Marking the old file would mean rewriting a finding after the fact, and the
        whole point of keeping them append-only is that what was recorded at the time
        stays recorded.
        """
        ids = set()
        for item in self.read_findings():
            target = item["front"].get("supersedes")
            if target:
                ids.update(part.strip() for part in str(target).split(",") if part.strip())
        return ids

    # -- rollup -----------------------------------------------------------

    def write_index(self) -> str:
        """A human-readable table of contents, regenerated on every write.

        Derived rather than authoritative, so it can always be deleted and rebuilt
        from the findings.
        """
        findings = self.read_findings()
        superseded = self.superseded_ids()
        target = self.meta.get("target") or "(unspecified)"
        title = self.meta.get("title") or self.pack_id

        lines = [
            f"# {title}",
            "",
            f"- Target: `{target}`",
            f"- Pack: `{self.pack_id}`",
            f"- Findings: {len(findings)}",
            f"- Updated: {self.meta.get('updated', '')}",
            "",
        ]

        open_questions = [
            f for f in findings
            if f["front"].get("kind") == "question" and f["front"].get("id") not in superseded
        ]
        if open_questions:
            lines += ["## Open questions", ""]
            for item in open_questions:
                lines.append(f"- [{item['front'].get('id')}] {item['front'].get('title')}")
            lines.append("")

        lines += ["## Findings", ""]
        if not findings:
            lines.append("_None yet._")
        for item in findings:
            front = item["front"]
            marks = [front.get("kind", "?"), front.get("confidence", "?")]
            if front.get("id") in superseded:
                marks.append("superseded")
            if front.get("location"):
                marks.append(front["location"])
            lines.append(f"- **{front.get('id')}** {front.get('title')} — {', '.join(marks)}")

        text = "\n".join(lines) + "\n"
        path = os.path.join(self.dir, "index.md")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
        return text


def _annotate(text: str, front: dict, superseded: set) -> str:
    """Prefix a banner when this finding has been retracted by a later one.

    Added to the returned *view*, never to the file: the file stays exactly as it was
    written, and the supersede relationship lives on the newer finding. But nothing
    inside the older file says it was retracted, so any read path that hands a
    finding back has to say so itself — otherwise the model reads a conclusion it
    already corrected and cites it as current, which is worse than not finding it at
    all.
    """
    if front.get("id") in superseded:
        return f"> SUPERSEDED by a later finding. Kept for the record; do not rely on it.\n\n{text}"
    return text


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _yaml_scalar(value: str) -> str:
    text = str(value).replace("\n", " ").strip()
    if any(ch in text for ch in ':#"\'') or text != text.strip():
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

def note_finding(
    pack: NotePack,
    kind: str = "",
    title: str = "",
    body: str = "",
    evidence: str = "",
    confidence: str = "",
    location: str = "",
    supersedes: str = "",
) -> ToolResult:
    """Record one finding. Refuses anything it could not justify later."""
    kind = str(kind or "").strip().lower()
    confidence = str(confidence or "").strip().lower()
    title = str(title or "").strip()

    if not title:
        return ToolResult(False, "note_finding needs a 'title'.")
    if kind not in KINDS:
        return ToolResult(
            False,
            f"'{kind or '(missing)'}' is not a finding kind. Use one of: "
            f"{', '.join(sorted(KINDS))}.",
        )
    if confidence not in CONFIDENCE:
        return ToolResult(
            False,
            f"'{confidence or '(missing)'}' is not a confidence level. Use one of: "
            f"{', '.join(CONFIDENCE)}. Say which it is rather than leaving it open — a "
            "guess recorded as a fact is worse than no note.",
        )

    body = str(body or "").strip()
    evidence = str(evidence or "").strip()

    # A question is allowed to have no answer yet; anything asserted is not, and that
    # includes a hypothesis — one with no stated basis cannot be tested later, which
    # is the only thing that makes recording it worthwhile.
    if kind not in EVIDENCE_OPTIONAL and not evidence:
        hint = (
            "For a hypothesis, 'evidence' is what you observed that suggests it, and the "
            "body should say what would confirm or refute it."
            if kind == "hypothesis" else
            "If you have not verified this at all, use kind 'question' to record it as an "
            "open unknown instead."
        )
        return ToolResult(
            False,
            "note_finding needs 'evidence': where you saw this — an offset, a symbol, a "
            "command and the part of its output that shows it. A finding nobody can "
            f"recheck is not worth recording. {hint}",
        )
    if not body:
        return ToolResult(False, "note_finding needs a 'body' saying what you found.")

    if supersedes:
        known = {item["front"].get("id") for item in pack.read_findings()}
        missing = [
            part.strip() for part in str(supersedes).split(",")
            if part.strip() and part.strip() not in known
        ]
        if missing:
            return ToolResult(
                False,
                f"Cannot supersede finding(s) {', '.join(missing)}: no such id in this pack. "
                f"Known ids: {', '.join(sorted(known)) or '(none)'}.",
            )

    try:
        record = pack.add(kind, title, body, evidence, confidence, location, supersedes)
    except OSError as exc:
        return ToolResult(False, f"Could not write the finding: {exc}")

    note = f" (supersedes {supersedes})" if supersedes else ""
    return ToolResult(
        True,
        f"Recorded finding {record['id']} [{kind}, {confidence}]: {title}{note}",
        "",
        {"finding": record, "pack": pack.pack_id},
    )


def read_notes(pack: NotePack, finding_id: str = "", kind: str = "") -> ToolResult:
    """Read the pack: the rollup, one finding, or everything of one kind.

    Exists so a folded transcript does not mean lost knowledge. The model can recover
    what it already established instead of re-deriving it.
    """
    findings = pack.read_findings()
    superseded = pack.superseded_ids()

    if finding_id:
        wanted = str(finding_id).strip().lstrip("#")
        for item in findings:
            if item["front"].get("id") == wanted or item["front"].get("id") == wanted.zfill(4):
                return ToolResult(
                    True,
                    f"Finding {item['front'].get('id')} from pack '{pack.pack_id}'.",
                    cap_text(_annotate(item["text"], item["front"], superseded), MAX_NOTES_CHARS),
                    {
                        "finding": item["front"],
                        "superseded": item["front"].get("id") in superseded,
                    },
                )
        known = ", ".join(sorted(i["front"].get("id", "?") for i in findings)) or "(none)"
        return ToolResult(False, f"No finding '{finding_id}' in this pack. Known ids: {known}.")

    if kind:
        wanted_kind = str(kind).strip().lower()
        if wanted_kind not in KINDS:
            return ToolResult(
                False, f"'{kind}' is not a finding kind. Use one of: {', '.join(sorted(KINDS))}."
            )
        subset = [i for i in findings if i["front"].get("kind") == wanted_kind]
        if not subset:
            return ToolResult(True, f"No '{wanted_kind}' findings recorded yet.", "")
        body = "\n\n---\n\n".join(
            _annotate(item["text"], item["front"], superseded) for item in subset
        )
        live = sum(1 for i in subset if i["front"].get("id") not in superseded)
        note = f" ({len(subset) - live} superseded)" if live != len(subset) else ""
        return ToolResult(
            True,
            f"{len(subset)} '{wanted_kind}' finding(s) in pack '{pack.pack_id}'{note}.",
            cap_text(body, MAX_NOTES_CHARS),
        )

    index = pack.write_index()
    return ToolResult(
        True,
        f"Pack '{pack.pack_id}': {len(findings)} finding(s).",
        cap_text(index, MAX_NOTES_CHARS),
        {"findings": len(findings), "pack": pack.pack_id},
    )


def open_pack(root: str, target: str = "", title: str = "", pack_id: str = "") -> NotePack:
    """Open or create the pack for a target.

    Never model-controlled: the loop chooses the pack, the same way it chooses the
    sandbox and the patch journal. A model that could pick the pack could scatter one
    investigation across several, or write into another one's.
    """
    chosen = pack_id or slugify(target or title, "investigation")
    if os.sep in chosen or chosen in ("", ".", ".."):
        raise NotesError(f"Unsafe pack id: {chosen!r}")
    return NotePack(root, chosen, target=target, title=title)


def list_packs(root: str) -> List[Dict[str, Optional[str]]]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    packs = []
    for name in names:
        meta_path = os.path.join(root, name, "pack.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = {}
        packs.append({
            "pack_id": name,
            "target": meta.get("target", ""),
            "title": meta.get("title", ""),
            "findings": meta.get("findings", 0),
            "updated": meta.get("updated", ""),
        })
    return packs
