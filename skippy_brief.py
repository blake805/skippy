"""Research briefs: one question, the pages read for it, and what they support.

The same argument as `skippy_re`, transplanted. A coding task leaves a diff and the
repository remembers it. A research run leaves nothing at all unless it is written
down: no file changes, and the transcript that held the sources gets folded and thrown
away. So the brief is the deliverable, and everything here follows from that.

**A source is a page as it was on the day we read it.** Logged by the loop after every
successful fetch, with its URL, its title, the date, and the text that actually came
back. Not a link — the text. A link is a promise that someone can go and check, and the
web breaks that promise routinely: pages get edited, paywalled, reorganised and
deleted. Keeping what the page said is what makes a claim recheckable a year later, and
it is also what lets the claim-recording tool verify a citation instead of trusting it.

**A claim must cite sources this run actually read.** `note_claim` refuses a citation
that does not match a logged source. This is the single most valuable constraint in the
module, and it exists because of the specific way models fail at this: asked for
citations, a model that cannot find one writes a plausible URL. A fabricated citation is
worse than no answer, because it is indistinguishable from a real one at a glance and it
launders a guess into a fact. Here it is a refusal the model can act on, listing what it
did read.

**Confidence is separate from citation, and 'confirmed' has to be earned.** One page
saying something is one page saying something — a single vendor blog and three sites
quoting that blog are the same source wearing four hats. `confirmed` requires two
sources on different hosts. It is a crude proxy for independence and it is stated as
one, but it is enough to stop the most common overclaim.

**Claims are append-only; a correction supersedes.** Same as a finding in a note pack
and for the same reason: research changes its mind, and the fact that it did — and on
what evidence — is part of the record.

**The answer is written at the end, from the claims, by a separate pass.** The loop that
gathered the sources is not the right thing to ask for prose: it has twenty pages of
untrusted page text in its context and a strong pull toward whatever it read last. The
synthesis pass sees the claims and the sources, and nothing else.

**Briefs go stale, and say so.** Sources carry the date they were read, and reopening a
brief on an old question warns before it hands anything back. Exactly the reasoning
behind the target digest in `skippy_re` and the stale-path marks in `skippy_memory`: an
unmarked out-of-date answer is worse than a missing one, and the web moves faster than
either of those.
"""

import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

# For the confidence vocabulary only. Restating the ordered list here would be a second
# source of truth of exactly the kind that goes stale quietly — the same reason
# skippy_memory borrows the severity list rather than copying it.
import skippy_re
from skippy_sandbox import ToolResult, cap_text

MAX_BRIEF_CHARS = 24_000
MAX_CLAIM_CHARS = 400
MAX_SUPPORT_CHARS = 4_000
MAX_ANSWER_CHARS = 16_000

# Per logged source. Generous for the same reason the RE command log is: this is the
# copy a person rechecks a claim against once the page has moved on, and a source
# trimmed to a sentence cannot do that job. Head and tail are both kept.
MAX_SOURCE_CHARS = 20_000

# Beyond this, a brief is old enough that the answer in it may describe a version of
# the world that no longer exists. Thirty days is a guess, and deliberately a
# conservative one: for anything current the warning firing too often costs a reread,
# while it failing to fire costs a wrong answer delivered confidently.
STALE_AFTER_DAYS = 30

# Cited as [S1], [S2]. Short and letter-prefixed on purpose: the answer is prose a
# person reads, and "[S1]" is a citation while "[0001]" is a serial number. Claims get
# C-numbers for the same reason, so a reference in a superseding claim cannot be
# mistaken for a source.
SOURCE_PREFIX = "S"
CLAIM_PREFIX = "C"

_ID_RE = re.compile(r"^[sc]?0*(\d+)$", re.IGNORECASE)


class BriefError(Exception):
    """The brief could not be read or written."""


def brief_id_for(question: str) -> str:
    """A stable id for a question: readable, plus a digest of the normalized text.

    Keyed by the question rather than by session so that asking the same thing again
    lands on the work already done — which is what makes a cache possible at all, and
    what stops the third identical question costing a third round of searches. The
    digest is over case-folded, punctuation-stripped words, so "What is the max feed
    rate?" and "what is the max feed rate" are one brief rather than two.
    """
    words = re.findall(r"[a-z0-9]+", str(question or "").lower())
    if not words:
        return "question"
    key = " ".join(words)
    name = skippy_re.slugify(" ".join(words[:8]), "question")[:40]
    return f"{name}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"


class Brief:
    """One question: the sources read for it, the claims they support, the answer."""

    def __init__(self, root: str, brief_id: str, question: str = ""):
        self.brief_id = brief_id
        self.dir = os.path.join(root, brief_id)
        self.sources_dir = os.path.join(self.dir, "sources")
        self.claims_dir = os.path.join(self.dir, "claims")
        self.meta_path = os.path.join(self.dir, "brief.json")
        self.answer_path = os.path.join(self.dir, "answer.md")
        os.makedirs(self.sources_dir, exist_ok=True)
        os.makedirs(self.claims_dir, exist_ok=True)

        self.meta = self._load_meta()
        changed = False
        # Only filled in, never overwritten: reopening must not relabel a brief that
        # already has claims under a differently-worded version of the same question.
        if question and not self.meta.get("question"):
            self.meta["question"] = question
            changed = True

        # Computed on open and reported everywhere the brief is handed back, rather
        # than stored: how stale it is depends on when you ask.
        self.stale = ""
        age = self.age_days()
        if age is not None and age >= STALE_AFTER_DAYS:
            self.stale = (
                f"These sources were read {age} day(s) ago. Anything here that depends on "
                "a current version, a price, a release or a person's role may since have "
                "changed: recheck it before relying on it."
            )

        if changed or not os.path.isfile(self.meta_path):
            self._save_meta()

    # -- metadata ---------------------------------------------------------

    @property
    def question(self) -> str:
        return str(self.meta.get("question") or "")

    def _load_meta(self) -> dict:
        if os.path.isfile(self.meta_path):
            try:
                with open(self.meta_path, encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                # A corrupt brief.json must not lose the sources and claims, which are
                # the valuable part and are stored as separate files.
                pass
        return {"brief_id": self.brief_id, "created": _now(), "question": ""}

    def _save_meta(self) -> None:
        self.meta["updated"] = _now()
        self.meta["sources"] = len(self.source_files())
        self.meta["claims"] = len(self.claim_files())
        tmp = f"{self.meta_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.meta, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.meta_path)

    def age_days(self) -> Optional[int]:
        """How long since the most recent source was read, or None with no sources."""
        newest = ""
        for entry in self.sources():
            fetched = str(entry["front"].get("fetched") or "")
            newest = max(newest, fetched)
        if not newest:
            return None
        try:
            when = time.mktime(time.strptime(newest[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
        return max(0, int((time.time() - when) / 86_400))

    # -- sources ----------------------------------------------------------

    def source_files(self) -> List[str]:
        return _markdown_files(self.sources_dir)

    def sources(self) -> List[dict]:
        return [entry for entry in _read_all(self.source_files())]

    def source_for(self, reference: str) -> Optional[dict]:
        """Find a source by id (S2, s2, 2) or by URL. None when we never read it."""
        wanted = str(reference or "").strip().rstrip(".,;)").strip()
        if not wanted:
            return None
        match = _ID_RE.match(wanted)
        number = match.group(1) if match else ""
        for entry in self.sources():
            front = entry["front"]
            if number and str(front.get("id", "")).lstrip(SOURCE_PREFIX) == number:
                return entry
            if wanted in (front.get("url"), front.get("final_url")):
                return entry
        return None

    def log_source(
        self,
        url: str,
        text: str,
        title: str = "",
        final_url: str = "",
        chunk: int = 1,
        chunks: int = 1,
    ) -> dict:
        """Record one page as it was read. Called by the loop, not offered to the model.

        Mechanical for the same reason the RE command log is: everything above this
        depends on the record existing, and a record the model has to remember to write
        is a record that a run dying at step nine does not have. It also means a
        citation can be checked against what the page actually said rather than against
        the model's recollection of it.

        Reading a second chunk of a page already logged extends that source instead of
        opening a new one — it is one page, and one citation.
        """
        final = str(final_url or url or "").strip()
        if not final:
            return {}

        existing = self.source_for(final)
        if existing is not None:
            return self._extend_source(existing, text, chunk)

        number = _next_number(self.source_files())
        source_id = f"{SOURCE_PREFIX}{number}"
        path = os.path.join(
            self.sources_dir, f"{number:04d}-{skippy_re.slugify(title or final, 'source')}.md"
        )

        front = {
            "id": source_id,
            "url": str(url or final),
            "fetched": _now(),
            "host": host_of(final),
        }
        if title:
            front["title"] = title
        if final != str(url or ""):
            front["final_url"] = final
        if chunks > 1:
            front["chunks_read"] = str(chunk)

        body = cap_text(str(text or "").strip(), MAX_SOURCE_CHARS)
        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# [{source_id}] {title or final}", "", final, "", body, ""]

        # OSError propagates: the loop catches it, because losing the log entry must
        # not fail the fetch it describes.
        _write(path, "\n".join(lines))
        self._save_meta()
        return {"id": source_id, "path": path, "url": final, "title": title}

    def _extend_source(self, entry: dict, text: str, chunk: int) -> dict:
        """Append a further chunk of a page already logged, and note which chunks."""
        front = entry["front"]
        read = {c.strip() for c in str(front.get("chunks_read") or "1").split(",") if c.strip()}
        if str(chunk) in read:
            return {"id": front.get("id"), "path": entry["path"], "url": front.get("url")}
        read.add(str(chunk))
        ordered = ", ".join(sorted(read, key=lambda c: int(c) if c.isdigit() else 0))

        text_block = f"\n\n--- chunk {chunk} ---\n\n{cap_text(str(text or '').strip(), MAX_SOURCE_CHARS)}\n"
        updated = entry["text"].rstrip() + text_block
        if "chunks_read:" in updated:
            updated = re.sub(r"(?m)^chunks_read: .*$", f"chunks_read: {ordered}", updated, count=1)
        else:
            updated = updated.replace("\n---\n", f"\nchunks_read: {ordered}\n---\n", 1)
        _write(entry["path"], updated)
        self._save_meta()
        return {"id": front.get("id"), "path": entry["path"], "url": front.get("url")}

    # -- claims -----------------------------------------------------------

    def claim_files(self) -> List[str]:
        return _markdown_files(self.claims_dir)

    def claims(self) -> List[dict]:
        return _read_all(self.claim_files())

    def add(
        self,
        claim: str,
        support: str,
        sources: List[dict],
        confidence: str,
        supersedes: str = "",
    ) -> dict:
        number = _next_number(self.claim_files())
        claim_id = f"{CLAIM_PREFIX}{number}"
        path = os.path.join(
            self.claims_dir, f"{number:04d}-{skippy_re.slugify(claim, 'claim')}.md"
        )

        cited = [str(s["front"].get("id")) for s in sources]
        front = {
            "id": claim_id,
            "claim": claim,
            "confidence": confidence,
            "sources": ", ".join(cited),
            "recorded": _now(),
        }
        if supersedes:
            front["supersedes"] = supersedes

        lines = ["---"]
        lines += [f"{key}: {_yaml_scalar(value)}" for key, value in front.items()]
        lines += ["---", "", f"# [{claim_id}] {claim}", ""]
        lines += [cap_text(support.strip(), MAX_SUPPORT_CHARS), "", "## Sources", ""]
        for entry in sources:
            sfront = entry["front"]
            lines.append(
                f"- [{sfront.get('id')}] {sfront.get('title') or sfront.get('url')} — "
                f"{sfront.get('final_url') or sfront.get('url')} (read {sfront.get('fetched')})"
            )
        lines.append("")

        _write(path, "\n".join(lines))
        self._save_meta()
        self.write_index()
        return {
            "id": claim_id,
            "path": path,
            "confidence": confidence,
            "sources": cited,
        }

    def superseded_ids(self) -> set:
        """Derived from the newer claims, so the older files are never rewritten."""
        ids = set()
        for entry in self.claims():
            target = entry["front"].get("supersedes")
            if target:
                ids.update(p.strip() for p in str(target).split(",") if p.strip())
        return ids

    # -- the answer -------------------------------------------------------

    def write_answer(self, text: str) -> str:
        """Store the synthesized answer beside the claims it was written from."""
        body = cap_text(str(text or "").strip(), MAX_ANSWER_CHARS)
        lines = [
            f"# {self.question or self.brief_id}",
            "",
            f"_Answered {_now()} from {len(self.source_files())} source(s)._",
            "",
            body,
            "",
        ]
        _write(self.answer_path, "\n".join(lines))
        self.meta["answered"] = _now()
        self._save_meta()
        return self.answer_path

    def read_answer(self) -> str:
        try:
            with open(self.answer_path, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    # -- rollup -----------------------------------------------------------

    def write_index(self) -> str:
        """A human-readable table of contents, regenerated on every write."""
        claims = self.claims()
        sources = self.sources()
        superseded = self.superseded_ids()

        lines = [
            f"# {self.question or self.brief_id}",
            "",
            f"- Brief: `{self.brief_id}`",
            f"- Sources read: {len(sources)}",
            f"- Claims recorded: {len(claims)}",
            f"- Updated: {self.meta.get('updated', '')}",
            "",
        ]
        if self.stale:
            lines += [f"> {self.stale}", ""]

        lines += ["## Claims", ""]
        if not claims:
            lines.append("_None yet._")
        for entry in claims:
            front = entry["front"]
            marks = [front.get("confidence", "?")]
            if front.get("id") in superseded:
                marks.append("superseded")
            lines.append(
                f"- **{front.get('id')}** {front.get('claim')} — {', '.join(marks)} "
                f"[{front.get('sources', '')}]"
            )
        lines += ["", "## Sources", ""]
        if not sources:
            lines.append("_None yet._")
        for entry in sources:
            front = entry["front"]
            lines.append(
                f"- **{front.get('id')}** {front.get('title') or front.get('url')} — "
                f"{front.get('final_url') or front.get('url')} (read {front.get('fetched')})"
            )

        text = "\n".join(lines) + "\n"
        _write(os.path.join(self.dir, "index.md"), text)
        return text

    def citation_block(self) -> str:
        """The sources, formatted for the synthesis pass and for a person to check."""
        lines = []
        for entry in self.sources():
            front = entry["front"]
            lines.append(
                f"[{front.get('id')}] {front.get('title') or front.get('url')} — "
                f"{front.get('final_url') or front.get('url')} (read {front.get('fetched')})"
            )
        return "\n".join(lines)

    def claims_block(self) -> str:
        """The claims with their support and citations, for the synthesis pass.

        Superseded claims are dropped rather than marked here: this text becomes an
        answer, and a retracted claim has no business appearing in one. The file keeps
        it, which is where the record of having changed our mind belongs.
        """
        superseded = self.superseded_ids()
        blocks = []
        for entry in self.claims():
            front = entry["front"]
            if front.get("id") in superseded:
                continue
            body = entry["text"].split("---", 2)[-1].strip()
            blocks.append(
                f"[{front.get('id')}] ({front.get('confidence')}) "
                f"cites {front.get('sources')}\n{body}"
            )
        return "\n\n".join(blocks)


def host_of(url: str) -> str:
    host = (urlparse(str(url or "")).hostname or "").lower()
    # www is not a different publisher, and treating it as one would let a claim reach
    # 'confirmed' by citing the same site twice.
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _markdown_files(directory: str) -> List[str]:
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".md"))
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names]


def _next_number(paths: List[str]) -> int:
    """The next counter for a directory of `NNNN-slug.md` files.

    Fixed width in the filename so that sorting the names is sorting by order of
    writing, which every read path here depends on.
    """
    highest = 0
    for path in paths:
        head = os.path.basename(path).split("-", 1)[0]
        if head.isdigit():
            highest = max(highest, int(head))
    return highest + 1


def _read_all(paths: List[str]) -> List[dict]:
    found = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        found.append({"path": path, "front": _parse_front(text), "text": text})
    return found


def _yaml_scalar(value) -> str:
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

def note_claim(
    brief: Brief,
    claim: str = "",
    support: str = "",
    sources: str = "",
    confidence: str = "",
    supersedes: str = "",
) -> ToolResult:
    """Record one claim and the sources behind it. Refuses a citation we cannot check."""
    claim = " ".join(str(claim or "").split())[:MAX_CLAIM_CHARS]
    support = str(support or "").strip()
    confidence = str(confidence or "").strip().lower()

    if not claim:
        return ToolResult(False, "note_claim needs a 'claim': the one thing you are asserting.")
    if confidence not in skippy_re.CONFIDENCE:
        return ToolResult(
            False,
            f"'{confidence or '(missing)'}' is not a confidence level. Use one of: "
            f"{', '.join(skippy_re.CONFIDENCE)}. Here 'confirmed' means two independent "
            "sources agree, 'likely' means one source you trust says so, and "
            "'speculative' means you are inferring it. Say which — a guess recorded as a "
            "fact is worse than no note.",
        )
    if not support:
        return ToolResult(
            False,
            "note_claim needs 'support': what the sources actually said, quoted or closely "
            "paraphrased. A claim with no support cannot be rechecked, and 'the page said "
            "so' is not something a later reader can act on.",
        )

    references = [r.strip() for r in str(sources or "").replace("\n", ",").split(",") if r.strip()]
    if not references:
        return ToolResult(
            False,
            "note_claim needs 'sources': the ids of the pages this rests on, like "
            "'S1, S3'. Every fetched page is logged with an id and the observation tells "
            "you which. If nothing you read supports this, do not record it — read "
            "something that does, or record it as speculative with the page that "
            "suggested it.",
        )

    resolved, missing = [], []
    seen = set()
    for reference in references:
        entry = brief.source_for(reference)
        if entry is None:
            missing.append(reference)
            continue
        identifier = entry["front"].get("id")
        if identifier in seen:
            continue
        seen.add(identifier)
        resolved.append(entry)

    if missing:
        # The refusal that matters most in this module. A model that cannot find a
        # citation writes a plausible one, and a fabricated URL is indistinguishable
        # from a real one to everybody downstream.
        known = ", ".join(
            f"{e['front'].get('id')} ({e['front'].get('final_url') or e['front'].get('url')})"
            for e in brief.sources()
        ) or "(none — you have not read any pages yet)"
        return ToolResult(
            False,
            f"Cannot cite {', '.join(missing)}: this run never read that. You may only "
            f"cite pages you actually fetched. Read so far: {known}.",
        )

    if confidence == "confirmed":
        hosts = {host_of(e["front"].get("final_url") or e["front"].get("url")) for e in resolved}
        if len(hosts) < 2:
            return ToolResult(
                False,
                "A 'confirmed' claim needs two sources on different sites. What you have "
                f"cited is {len(hosts)} site, and a vendor blog plus three pages quoting "
                "that blog is one source wearing four hats. Either cite a second, "
                "independent page or record this as 'likely'.",
            )

    if supersedes:
        known = {entry["front"].get("id") for entry in brief.claims()}
        absent = [
            part.strip() for part in str(supersedes).split(",")
            if part.strip() and part.strip() not in known
        ]
        if absent:
            return ToolResult(
                False,
                f"Cannot supersede claim(s) {', '.join(absent)}: no such id in this brief. "
                f"Known ids: {', '.join(sorted(i for i in known if i)) or '(none)'}.",
            )

    try:
        record = brief.add(claim, support, resolved, confidence, supersedes)
    except OSError as exc:
        return ToolResult(False, f"Could not write the claim: {exc}")

    note = f" (supersedes {supersedes})" if supersedes else ""
    return ToolResult(
        True,
        f"Recorded claim {record['id']} [{confidence}] citing "
        f"{', '.join(record['sources'])}: {claim}{note}",
        "",
        {"claim": record, "brief": brief.brief_id},
    )


def read_brief(brief: Brief, section: str = "") -> ToolResult:
    """Read what this brief already holds: the rollup, the sources, or the claims.

    Exists so a folded transcript does not mean lost work, and so a question asked
    again opens with what was already established rather than searching for it twice.
    """
    wanted = str(section or "").strip().lower()
    stale = f"> {brief.stale}\n\n" if brief.stale else ""

    if wanted in ("source", "sources"):
        sources = brief.sources()
        if not sources:
            return ToolResult(True, "No sources read yet in this brief.", "")
        body = stale + "\n\n---\n\n".join(entry["text"] for entry in sources)
        return ToolResult(
            True,
            f"{len(sources)} source(s) in brief '{brief.brief_id}'.",
            cap_text(body, MAX_BRIEF_CHARS),
            {"sources": len(sources)},
        )

    if wanted in ("claim", "claims"):
        claims = brief.claims()
        if not claims:
            return ToolResult(True, "No claims recorded yet in this brief.", "")
        superseded = brief.superseded_ids()
        blocks = []
        for entry in claims:
            text = entry["text"]
            if entry["front"].get("id") in superseded:
                # Marked on the way out rather than in the file, for the same reason a
                # superseded finding is: the file records what was believed at the time,
                # and handing it back unmarked would have the model cite a retraction.
                text = f"> SUPERSEDED by a later claim. Kept for the record.\n\n{text}"
            blocks.append(text)
        return ToolResult(
            True,
            f"{len(claims)} claim(s) in brief '{brief.brief_id}'.",
            cap_text(stale + "\n\n---\n\n".join(blocks), MAX_BRIEF_CHARS),
            {"claims": len(claims)},
        )

    if wanted and wanted not in ("index", "all"):
        return ToolResult(
            False,
            f"'{section}' is not a section of a brief. Use 'sources', 'claims', or leave "
            "it out for the index.",
        )

    # The index carries the staleness banner itself, so it is not prefixed again.
    index = brief.write_index()
    answer = brief.read_answer()
    if answer:
        index += "\n\n## Previous answer\n\n" + answer
    return ToolResult(
        True,
        f"Brief '{brief.brief_id}': {len(brief.source_files())} source(s), "
        f"{len(brief.claim_files())} claim(s).",
        cap_text(index, MAX_BRIEF_CHARS),
        {
            "brief": brief.brief_id,
            "sources": len(brief.source_files()),
            "claims": len(brief.claim_files()),
            "answered": bool(answer),
        },
    )


def open_brief(root: str, question: str = "", brief_id: str = "") -> Brief:
    """Open or create the brief for a question.

    Never model-controlled, for the same reason as the sandbox and the note pack: a
    model that could pick the brief could append this question's sources to another
    question's answer.
    """
    chosen = brief_id or brief_id_for(question)
    if os.sep in chosen or chosen in ("", ".", ".."):
        raise BriefError(f"Unsafe brief id: {chosen!r}")
    return Brief(root, chosen, question=question)


def list_briefs(root: str) -> List[Dict[str, object]]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    briefs = []
    for name in names:
        meta_path = os.path.join(root, name, "brief.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = {}
        briefs.append({
            "brief_id": name,
            "question": meta.get("question", ""),
            "sources": meta.get("sources", 0),
            "claims": meta.get("claims", 0),
            "answered": meta.get("answered", ""),
            "updated": meta.get("updated", ""),
        })
    return briefs
