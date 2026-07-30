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

**The loop records the commands; the model records the findings.** Everything above
depends on the model choosing to write things down, and the first live run showed it
choosing to do so in a batch at the end — so a run dying midway would have left
nothing, despite an explicit instruction to record as it went. Asking harder is not
the fix. Every inspection command and its output is appended to the pack by the loop
as it runs, which makes the evidence durable without asking, and leaves findings as
what they should be: the model's judgment about evidence that already exists on disk.
It also means a finding's stated evidence can be checked against what the command
actually printed, rather than against the model's recollection of it.

**A weakness is a finding with a fix attached.** Findings here exist to drive changes
to our own code, so the one kind that has a destination outside the pack is
`weakness`: it carries a severity, and the loop turns it into a work item in project
memory so that a later coding session opens already knowing about it. Find it in RE
mode, fix it in coding mode.
"""

import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from skippy_sandbox import ToolResult, cap_text

MAX_NOTES_CHARS = 24_000
MAX_BODY_CHARS = 8_000
MAX_EVIDENCE_CHARS = 4_000

# Per logged command. Generous, because the log is the durable record a person
# rechecks a finding against, and a truncated `objdump` region is exactly the case
# where "recheck it yourself" has to still be possible. Head and tail are both kept.
MAX_COMMAND_OUTPUT_CHARS = 20_000

# Above this, the target is fingerprinted by sampling rather than read end to end.
# A pack is opened at the start of every run, and hashing a multi-gigabyte flash
# image before the first step would be latency charged to every session.
DIGEST_FULL_LIMIT = 256 * 1024 * 1024
DIGEST_SAMPLE_BYTES = 4 * 1024 * 1024

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
    "weakness",    # something that should be fixed, with a severity
}

# Ordered weakest-first; the ordering is used when reporting a pack's overall
# standing, where the weakest link is what matters.
CONFIDENCE = ("speculative", "likely", "confirmed")

# Ordered least-urgent-first. This is fix urgency in our own code, not a CVSS score:
# the finding exists to get something changed, and the question a severity has to
# answer is "does this go in front of the current sprint or behind it". A numeric
# score would imply a comparability we cannot supply from static inspection, and
# would invite arguing about the number instead of doing the fix.
SEVERITY = ("low", "medium", "high", "critical")

# Severity is meaningless without confidence beside it. A speculative critical and a
# confirmed critical are different work, and showing severity alone is how a guess
# acquires a deadline — the same failure the confidence field already exists to stop.
KINDS_REQUIRING_SEVERITY = frozenset({"weakness"})

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


def target_digest(target: str) -> Tuple[str, str]:
    """Fingerprint the target's bytes, returning (digest, method).

    Recorded once when a pack first names its target, and compared on every later
    open. The point is not integrity in a security sense — it is that pointing a pack
    at a rebuilt image otherwise presents last month's findings about last month's
    bytes as current, with nothing anywhere saying the artifact moved underneath them.
    Same reasoning as marking a stale path in `skippy_memory` or a superseded finding
    here: an unmarked wrong answer is worse than a missing one.

    Large targets are sampled rather than read whole, because this runs before the
    first step of every session and a flash image is not a small file. The method is
    returned alongside the digest so that two digests are only ever compared when
    they were computed the same way.
    """
    path = os.path.expanduser(str(target or "").strip())
    if not path or not os.path.isfile(path):
        # A target need not be a file at all — it may name a device or a board — and
        # that is not an error, it just means there is nothing to fingerprint.
        return "", ""

    try:
        size = os.path.getsize(path)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            if size <= DIGEST_FULL_LIMIT:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                method = "sha256"
            else:
                # Size is folded in, so a truncation that leaves both ends intact
                # still changes the digest.
                digest.update(str(size).encode("ascii"))
                digest.update(handle.read(DIGEST_SAMPLE_BYTES))
                handle.seek(max(0, size - DIGEST_SAMPLE_BYTES))
                digest.update(handle.read(DIGEST_SAMPLE_BYTES))
                method = "sha256-sampled"
    except OSError:
        # An unreadable target must not stop a session that can still read the notes.
        return "", ""
    return digest.hexdigest(), method


def pack_id_for(target: str = "", title: str = "") -> str:
    """The pack id for a target: a readable name, plus a digest of where it lives.

    This used to be the slugified target alone, which meant two of our own products
    that both ship a `firmware.bin` collided into one pack. The second investigation
    then opened the first one's findings as its context and appended to it — the worst
    available outcome, because nothing looks wrong: the session starts with confident
    prior knowledge about a different product.

    The resolved path is what distinguishes them, so the id carries a digest of it.
    The basename stays on the front because a directory of packs has to be navigable
    by eye. Keyed by path rather than by content on purpose: a rebuilt image is the
    same investigation and should accumulate, and the digest recorded in `pack.json`
    is what reports that the bytes moved.
    """
    text = str(target or "").strip()
    if not text:
        return slugify(title, "investigation")
    name = slugify(os.path.basename(text.rstrip(os.sep)) or text, "target")[:40]
    key = os.path.realpath(os.path.expanduser(text))
    return f"{name}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"


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
        self.commands_dir = os.path.join(self.dir, "commands")
        self.meta_path = os.path.join(self.dir, "pack.json")
        os.makedirs(self.findings_dir, exist_ok=True)
        os.makedirs(self.commands_dir, exist_ok=True)

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

        # Recorded once, then compared — never refreshed. Refreshing would silently
        # adopt whatever bytes are there now, which is precisely the event this is
        # meant to report.
        self.target_changed = ""
        if target:
            digest, method = target_digest(target)
            if digest and not self.meta.get("target_digest"):
                self.meta["target_digest"] = digest
                self.meta["digest_method"] = method
                changed = True
            elif digest and method == self.meta.get("digest_method"):
                if digest != self.meta.get("target_digest"):
                    self.target_changed = (
                        "The target's bytes have changed since this pack was written "
                        f"({self.meta.get('digest_method')} digest "
                        f"{str(self.meta.get('target_digest'))[:12]} then, "
                        f"{digest[:12]} now). Findings below describe the earlier "
                        "bytes; recheck anything you rely on before building on it."
                    )
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
        self.meta["commands"] = len(self.command_files())
        tmp = f"{self.meta_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.meta, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.meta_path)

    # -- findings ---------------------------------------------------------

    def finding_files(self) -> List[str]:
        return _markdown_files(self.findings_dir)

    def next_id(self) -> str:
        return _next_sequence(self.finding_files())

    def add(
        self,
        kind: str,
        title: str,
        body: str,
        evidence: str,
        confidence: str,
        location: str = "",
        supersedes: str = "",
        severity: str = "",
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
        if severity:
            front["severity"] = severity
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
        record = {"id": finding_id, "path": path, "kind": kind, "confidence": confidence}
        if severity:
            record["severity"] = severity
        return record

    # -- the command log --------------------------------------------------

    def command_files(self) -> List[str]:
        return _markdown_files(self.commands_dir)

    def log_command(
        self,
        command: str,
        output: str = "",
        cwd: str = "",
        exit_code: Optional[int] = None,
        ok: Optional[bool] = None,
    ) -> dict:
        """Append one inspection command and what it printed.

        Called by the loop after every command in RE mode, not offered to the model.
        The durability argument for the notes applies with more force here: findings
        are written when the model decides to write them, and the first live run
        decided to write them all at the end, so a run dying at step nine would have
        left nothing behind. Recording the commands mechanically means the evidence
        survives regardless of what the model does with its budget.

        Written one file per command rather than appended to a single log, so that a
        run killed mid-command still leaves every earlier one intact and a large
        region does not make the rest unreadable.
        """
        command = str(command or "").strip()
        if not command:
            return {}

        command_id = _next_sequence(self.command_files())
        path = os.path.join(
            self.commands_dir, f"{command_id}-{slugify(command, 'command')}.md"
        )

        front = {"id": command_id, "command": command, "recorded": _now()}
        if cwd:
            front["cwd"] = cwd
        if exit_code is not None:
            front["exit_code"] = exit_code
        if ok is not None:
            front["ok"] = "true" if ok else "false"

        body = cap_text(str(output or "").strip(), MAX_COMMAND_OUTPUT_CHARS)
        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# `{command}`", "", "```", body or "(no output)", "```", ""]

        # OSError propagates: the loop catches it, because losing a log entry must not
        # fail the command it describes.
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.replace(tmp, path)

        self._save_meta()
        return {"id": command_id, "path": path, "command": command}

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
            f"- Commands logged: {len(self.command_files())}",
            f"- Updated: {self.meta.get('updated', '')}",
            "",
        ]
        if self.target_changed:
            lines += [f"> {self.target_changed}", ""]

        # Weaknesses first, worst first. They are the only kind with somewhere else to
        # go — a work item in project memory — so a person reading the pack should see
        # what is outstanding before the descriptive material.
        weaknesses = [
            f for f in findings
            if f["front"].get("kind") == "weakness" and f["front"].get("id") not in superseded
        ]
        if weaknesses:
            weaknesses.sort(key=lambda f: severity_rank(f["front"].get("severity")), reverse=True)
            lines += ["## Weaknesses", ""]
            for item in weaknesses:
                front = item["front"]
                lines.append(
                    f"- [{front.get('id')}] **{front.get('severity', '?')}** "
                    f"({front.get('confidence', '?')}) {front.get('title')}"
                )
            lines.append("")

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
            if front.get("severity"):
                marks.append(front["severity"])
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


def severity_rank(severity: Optional[str]) -> int:
    """Position in SEVERITY, or -1 for anything unrecognised.

    -1 rather than 0 so that a missing severity sorts below `low` instead of tying
    with it: "nobody said" and "somebody said this is minor" are different claims.
    """
    try:
        return SEVERITY.index(str(severity or "").strip().lower())
    except ValueError:
        return -1


def _markdown_files(directory: str) -> List[str]:
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".md"))
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names]


def _next_sequence(paths: List[str]) -> str:
    """The next fixed-width counter for a directory of `NNNN-slug.md` files.

    Fixed width so that sorting the filenames is sorting by order of writing, which
    is what both `finding_files` and `command_files` depend on.
    """
    highest = 0
    for path in paths:
        head = os.path.basename(path).split("-", 1)[0]
        if head.isdigit():
            highest = max(highest, int(head))
    return f"{highest + 1:04d}"


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
    severity: str = "",
) -> ToolResult:
    """Record one finding. Refuses anything it could not justify later."""
    kind = str(kind or "").strip().lower()
    confidence = str(confidence or "").strip().lower()
    severity = str(severity or "").strip().lower()
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

    # Checked before the required-severity test below, so that a misspelled severity
    # is reported as a misspelling rather than as an absence.
    if severity and severity not in SEVERITY:
        return ToolResult(
            False,
            f"'{severity}' is not a severity. Use one of: {', '.join(SEVERITY)}. This is "
            "how urgently it should be fixed in our own code, not a CVSS score.",
        )
    if kind in KINDS_REQUIRING_SEVERITY and not severity:
        return ToolResult(
            False,
            f"A '{kind}' finding needs a 'severity': one of {', '.join(SEVERITY)}. It "
            "becomes a work item for a later coding session, and one with no severity "
            "cannot be ordered against the other work waiting there. Judge how urgently "
            "it should be fixed; 'confidence' separately records how sure you are that "
            "it is real.",
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
            "command and the part of its output that shows it, or a file and byte range "
            "in an artifact you read. A finding nobody can recheck is not worth "
            f"recording. {hint}",
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
        record = pack.add(
            kind, title, body, evidence, confidence, location, supersedes, severity
        )
    except OSError as exc:
        return ToolResult(False, f"Could not write the finding: {exc}")

    note = f" (supersedes {supersedes})" if supersedes else ""
    marks = f"{kind}, {confidence}" + (f", {severity}" if severity else "")
    return ToolResult(
        True,
        f"Recorded finding {record['id']} [{marks}]: {title}{note}",
        "",
        # `finding` carries the severity, which is how the loop knows to raise a work
        # item without re-reading the file it just wrote.
        {"finding": record, "pack": pack.pack_id},
    )


def read_notes(pack: NotePack, finding_id: str = "", kind: str = "") -> ToolResult:
    """Read the pack: the rollup, one finding, or everything of one kind.

    Exists so a folded transcript does not mean lost knowledge. The model can recover
    what it already established instead of re-deriving it.
    """
    findings = pack.read_findings()
    superseded = pack.superseded_ids()
    # Prepended on every path for the same reason the supersede banner is: a finding
    # handed back with nothing saying the artifact changed underneath it reads as
    # current, and the model will cite it as such.
    stale = f"> {pack.target_changed}\n\n" if pack.target_changed else ""

    if finding_id:
        wanted = str(finding_id).strip().lstrip("#")
        for item in findings:
            if item["front"].get("id") == wanted or item["front"].get("id") == wanted.zfill(4):
                view = stale + _annotate(item["text"], item["front"], superseded)
                return ToolResult(
                    True,
                    f"Finding {item['front'].get('id')} from pack '{pack.pack_id}'.",
                    cap_text(view, MAX_NOTES_CHARS),
                    {
                        "finding": item["front"],
                        "superseded": item["front"].get("id") in superseded,
                        "target_changed": bool(pack.target_changed),
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
        body = stale + "\n\n---\n\n".join(
            _annotate(item["text"], item["front"], superseded) for item in subset
        )
        live = sum(1 for i in subset if i["front"].get("id") not in superseded)
        note = f" ({len(subset) - live} superseded)" if live != len(subset) else ""
        return ToolResult(
            True,
            f"{len(subset)} '{wanted_kind}' finding(s) in pack '{pack.pack_id}'{note}.",
            cap_text(body, MAX_NOTES_CHARS),
            {"target_changed": bool(pack.target_changed)},
        )

    # The index carries the banner itself, so it is not prefixed again here.
    index = pack.write_index()
    return ToolResult(
        True,
        f"Pack '{pack.pack_id}': {len(findings)} finding(s).",
        cap_text(index, MAX_NOTES_CHARS),
        {
            "findings": len(findings),
            "pack": pack.pack_id,
            "commands": len(pack.command_files()),
            "target_changed": bool(pack.target_changed),
        },
    )


def open_pack(root: str, target: str = "", title: str = "", pack_id: str = "") -> NotePack:
    """Open or create the pack for a target.

    Never model-controlled: the loop chooses the pack, the same way it chooses the
    sandbox and the patch journal. A model that could pick the pack could scatter one
    investigation across several, or write into another one's.
    """
    chosen = pack_id or pack_id_for(target, title)
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
            "commands": meta.get("commands", 0),
            "updated": meta.get("updated", ""),
        })
    return packs
