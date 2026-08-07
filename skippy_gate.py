"""Deciding, without being asked, when an answer needs checking.

The capability in `skippy_research` is only worth having if it fires on its own. A tool
the user has to ask for is a tool they will remember to ask for exactly when they
already suspect the answer is wrong — which is the one case where they did not need the
help. What is actually wanted is the assistant noticing.

**Why this is layered.** The obvious implementation is to ask the model whether it is
sure. That does not work: self-reported confidence is poorly calibrated, and it is
worst in exactly the situation that matters, where the model is fluent and wrong. So
three signals, none of them trusted alone:

1. **Cheap heuristics** (`signals`). Regexes over the turn. Recency words, years,
   version numbers, prices — and, on the other side, the vocabulary of thinking out
   loud. These run first because they are free and because they can settle the easy
   cases without a model call at all, which is the same reason `wants_action` gates the
   voice router.
2. **A fast-model gate before the answer** (`pre_answer`). The `VOICE_ROUTER` pattern:
   a cold, cheap, three-way classification. It has no stake in the answer, which is
   precisely what makes it better than asking the answering model.
3. **The answering model's own verdict afterwards** (`post_answer`). Last, and only as
   an escalation. What it is actually good at is not the number but the list: which
   statements it just made are the kind of thing that could be wrong. The number gets a
   threshold, tuned against a labelled set rather than guessed.

**Biased toward answering.** A false "research" interrupts someone's train of thought
to go and read the internet; a false "answer" leaves things exactly as they were before
any of this existed. The two costs are not symmetric, so the gate is not either. Opinion
and ideation are never researched — a question with no factual answer cannot be settled
by looking it up.

**The user is always right about this.** "Just tell me" turns it off for that turn and
"go check that" turns it on, both without a model call. Nothing here should ever argue
with an explicit instruction.

**Nothing blocks.** The conversation answers immediately, the research runs behind it,
and the follow-up arrives when it arrives. A gate that made the user wait would be worse
than no gate: they asked a person a question, not a search engine.

**Budgets and caching, because this spends money and time on its own.** Three runs per
conversation, five sources per run, and a question already answered — in this
conversation, in a brief on disk, or in project memory — is not researched again.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import prompts
import skippy_brief
import skippy_llm
import skippy_paths
import skippy_research

logger = logging.getLogger("skippy_gate")

AUTO_ENV = "SKIPPY_AUTO_RESEARCH"
CONFIDENCE_ENV = "SKIPPY_RESEARCH_CONFIDENCE"

# Below this, the answering model's own verdict escalates to a check.
#
# Derived, not chosen. `python -m tests.gate_eval` sweeps this against the labelled
# turns in tests/fixtures/gate_cases.json under a cost model that prices a missed check
# at twice a needless one, and 0.75 is where that curve bottoms out; a test fails if
# this constant drifts away from what the set says. The shape either side is the
# interesting part — below it, honest hedging goes unchecked, and above 0.8 it starts
# spending runs re-verifying answers that were already right.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

# Per conversation, not per turn. Three is enough to check the two or three things that
# actually come up in a working session and low enough that a conversation about a
# fast-moving subject does not turn into a search engine with a personality.
DEFAULT_MAX_RUNS = 3
# Per run. The synthesis is better from five sources read properly than from twelve
# skimmed, and this is also the difference between a follow-up in one minute and one in
# five — by which time the conversation has moved on and the answer is an interruption.
DEFAULT_MAX_SOURCES = 5

# The gate never runs on something this short. "yes", "ok", "thanks" and "hm" are
# turns, and classifying them costs a model call to be told what is obvious.
MIN_TURN_CHARS = 12

# Explicit instructions, checked before anything else and never overridden. Cheap, and
# more importantly not a judgment: the user said what they wanted.
_NEVER = re.compile(
    r"\b(just (tell|answer|give)|off the top of your head|don'?t (look|search|check|"
    r"research)|no need to (look|search|check)|your best guess|guess|gut feel|"
    r"without (looking|checking|searching))\b",
    re.IGNORECASE,
)
_ALWAYS = re.compile(
    # Both orders of the separable verb. "look it up" was the only one the first draft
    # matched, and the labelled set caught "look up what the torque is" going unheard.
    r"\b(look (it|that|this|them) up|look up |go (and )?check|check (that|this|it|whether|if)|"
    r"search (for|the web|online)|google|find out|verify|fact.?check|double.?check|"
    r"what does the (doc|documentation|spec|standard|manual|datasheet) say|"
    r"do some research|research (it|that|this))\b",
    re.IGNORECASE,
)

# Things whose answer has a date on it. The strongest cheap signal there is: a model
# cannot know what happened after it was trained, and it does not feel any less certain
# about that than about anything else.
_RECENCY = re.compile(
    r"\b(latest|newest|current(ly)?|these days|nowadays|right now|today|this (year|"
    r"month|week)|recent(ly)?|as of|still (supported|maintained|available|work)|"
    r"deprecated|discontinued|end.of.life|released?|release date|version|changelog|"
    r"price|pricing|costs?|how much|in stock|available)\b",
    re.IGNORECASE,
)
# A version number or a year is a fact with an expiry date, whoever said it.
_VERSIONISH = re.compile(r"\b(v?\d+\.\d+(\.\d+)?|20[12]\d)\b")
# Someone thinking out loud. These are the turns where checking is not just unnecessary
# but actively rude, so they count against researching rather than merely not for it.
_IDEATION = re.compile(
    r"\b(what if|imagine|suppose|brainstorm|do you (think|reckon)|your (opinion|take)|"
    r"thoughts\?|should (i|we)|would you|which would|how would you|i'?m thinking|"
    r"idea|design|sketch|feels?|prefer|better in your view|talk me through your)\b",
    re.IGNORECASE,
)
# Hedging in the answer, not in the question. This is the model telling on itself in
# prose, which it does far more reliably than it reports a number.
_HEDGE = re.compile(
    r"\b(i think|i believe|as far as i know|if i (recall|remember)|iirc|probably|"
    r"might be|may be|i'?m not (sure|certain)|not entirely sure|last i checked|"
    r"off the top of my head|may have changed|i could be wrong|around|roughly|"
    r"something like)\b",
    re.IGNORECASE,
)
# A capitalized word mid-sentence: a product, a company, a standard. Weak on its own —
# it also matches names and the starts of quotes — so it only ever adds to a score.
_PROPER_NOUN = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z0-9]{2,}(?:\s[A-Z][a-zA-Z0-9]+)?)\b")
_COMMON_CAPS = frozenset({
    "I", "I'm", "I'd", "Skippy", "Sarah", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May",
    "June", "July", "August", "September", "October", "November", "December", "OK",
})


def auto_enabled() -> bool:
    """Whether the gate may fire on its own.

    Off when there is no search backend configured, which is not a fallback so much as
    the only sane behaviour: without a key every autonomous check would end in a
    follow-up apologising for not being able to check, which is worse than never having
    offered. An explicit request still reaches the research tools and still gets that
    message, because then somebody asked.
    """
    if os.environ.get(AUTO_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    try:
        return skippy_research.build_backend().configured
    except skippy_research.ResearchError:
        return False


def confidence_threshold() -> float:
    raw = os.environ.get(CONFIDENCE_ENV, "").strip()
    if not raw:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", CONFIDENCE_ENV, raw)
        return DEFAULT_CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@dataclass
class Signals:
    """What the cheap layer noticed, and why."""

    recency: bool = False
    versionish: bool = False
    ideation: bool = False
    hedged: bool = False
    proper_nouns: List[str] = field(default_factory=list)
    override: str = ""  # "never" | "always" | ""

    @property
    def score(self) -> int:
        """How much the text on its own argues for checking.

        Deliberately crude. This decides whether the classifier call is worth making,
        not whether to research — the one place it decides anything alone is when it is
        zero and the turn reads as ideation.
        """
        total = 0
        total += 2 if self.recency else 0
        total += 1 if self.versionish else 0
        total += 1 if self.proper_nouns else 0
        total += 2 if self.hedged else 0
        total -= 2 if self.ideation else 0
        return total

    @property
    def reason(self) -> str:
        parts = []
        if self.recency:
            parts.append("asks about something current")
        if self.versionish:
            parts.append("names a version or a year")
        if self.proper_nouns:
            parts.append(f"names {', '.join(self.proper_nouns[:3])}")
        if self.hedged:
            parts.append("the answer hedged")
        if self.ideation:
            parts.append("reads as thinking out loud")
        return "; ".join(parts)


def signals(text: str, answer: str = "") -> Signals:
    """Read a turn (and optionally the answer given to it) for cheap signals."""
    text = str(text or "")
    found = Signals(
        recency=bool(_RECENCY.search(text)),
        versionish=bool(_VERSIONISH.search(text)),
        ideation=bool(_IDEATION.search(text)),
        hedged=bool(_HEDGE.search(str(answer or ""))),
        proper_nouns=[
            match.group(1) for match in _PROPER_NOUN.finditer(text)
            if match.group(1) not in _COMMON_CAPS
        ][:5],
    )
    # Checked last so it wins: an instruction is not a signal to be weighed against
    # other signals.
    if _ALWAYS.search(text):
        found.override = "always"
    elif _NEVER.search(text):
        found.override = "never"
    return found


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    research: bool
    question: str = ""
    reason: str = ""
    # Which layer decided, so a log line — or the Phase 4 eval — can say which part of
    # this is earning its keep.
    layer: str = ""

    def __bool__(self) -> bool:
        return self.research


def _parse(raw: str) -> Optional[dict]:
    """The one JSON object in a classifier's reply, or None.

    Malformed output downgrades to "no decision" rather than raising, for the reason
    `parse_route` gives in skippy_voice: these are small models at temperature zero, and
    the cost of one having a bad day should be an unchecked answer, not a broken turn.
    """
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def pre_answer(
    text: str,
    history: Sequence[dict] = (),
    role: str = "fast",
    timeout: float = 20.0,
) -> Decision:
    """Decide, before answering, whether this turn needs checking."""
    text = str(text or "").strip()
    found = signals(text)

    if found.override == "never":
        return Decision(False, reason="the user asked for an answer, not a search",
                        layer="override")
    if found.override == "always":
        return Decision(True, question=text, reason="the user asked for it",
                        layer="override")
    if not auto_enabled():
        return Decision(False, reason="autonomous research is off", layer="config")
    if len(text) < MIN_TURN_CHARS:
        return Decision(False, reason="too short to be a question", layer="heuristic")
    if found.score <= 0:
        # Nothing in the turn points at a fact with a date on it, so the classifier is
        # not worth the latency it would add to every reply — and it would be added to
        # every reply, including "good morning". This is the same trade `wants_action`
        # makes before the voice router, and it is safe for the same reason it is safe
        # there: missing here is not the last chance. The self-check runs behind the
        # delivered answer, costs the user nothing, and catches what a turn's wording
        # did not advertise.
        return Decision(False, reason=found.reason or "nothing to date it", layer="heuristic")

    recent = "\n".join(
        f"{turn.get('role')}: {turn.get('content')}" for turn in list(history)[-4:]
    ) or "(none)"
    try:
        raw = await skippy_llm.query_text(
            [
                {"role": "system", "content": prompts.RESEARCH_GATE},
                {"role": "user", "content": f"Recent turns:\n{recent}\n\nLatest: {text}"},
            ],
            role=role,
            temp=0.0,
            max_tokens=200,
            attempts=1,
            timeout=timeout,
        )
    except Exception as exc:
        # An unreachable classifier means the assistant answers as it always did.
        logger.warning("Research gate unavailable: %s", exc)
        return Decision(False, reason=f"gate unavailable ({exc})", layer="classifier")

    parsed = _parse(raw) or {}
    decision = str(parsed.get("decision") or "").strip().lower()
    if decision != "research":
        return Decision(False, reason=decision or "unparseable", layer="classifier")
    question = " ".join(str(parsed.get("question") or text).split())
    return Decision(True, question=question, reason=found.reason or "needs current sources",
                    layer="classifier")


async def post_answer(
    text: str,
    answer: str,
    role: str = "fast",
    timeout: float = 20.0,
) -> Decision:
    """Decide, after answering, whether what was just said should be checked.

    Runs behind a reply that has already been delivered, so its cost is not latency the
    user waits through — which is what makes it affordable to ask the answering model
    itself rather than a cheaper one.
    """
    text = str(text or "").strip()
    answer = str(answer or "").strip()
    if not auto_enabled() or not answer:
        return Decision(False, layer="config")

    found = signals(text, answer)
    if found.override:
        # Already honoured before the answer. Asking again would let a "just tell me"
        # turn into a check the user explicitly declined.
        return Decision(False, reason="the user was explicit", layer="override")

    try:
        raw = await skippy_llm.query_text(
            [
                {"role": "system", "content": prompts.RESEARCH_SELF_CHECK},
                {"role": "user", "content": f"They asked: {text}\n\nYou answered: {answer}"},
            ],
            role=role,
            temp=0.0,
            max_tokens=300,
            attempts=1,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Self-check unavailable: %s", exc)
        return Decision(False, reason=f"self-check unavailable ({exc})", layer="self-check")

    parsed = _parse(raw)
    if parsed is None:
        return Decision(False, reason="unparseable self-check", layer="self-check")

    try:
        confidence = float(parsed.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    checkable = [str(c).strip() for c in (parsed.get("checkable") or []) if str(c).strip()]
    question = " ".join(str(parsed.get("question") or "").split())

    # Both halves are required. A low number with nothing checkable behind it is a model
    # being modest about an opinion, and researching that finds nothing; checkable claims
    # with high confidence are the ordinary case of knowing something.
    if confidence >= confidence_threshold() or not checkable:
        return Decision(
            False,
            reason=f"self-reported {confidence:.2f} with {len(checkable)} checkable claim(s)",
            layer="self-check",
        )
    if not question:
        question = text
    return Decision(
        True,
        question=question,
        reason=f"self-reported {confidence:.2f}: {checkable[0][:120]}",
        layer="self-check",
    )


# ---------------------------------------------------------------------------
# Per-conversation state
# ---------------------------------------------------------------------------

@dataclass
class Result:
    """What a check produced, and where it came from."""

    question: str
    answer: str = ""
    brief_id: str = ""
    sources: int = 0
    # True when nothing was searched: the answer came from this conversation, from a
    # brief on disk, or from project memory.
    cached: bool = False
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.answer)


class Conversation:
    """One conversation's research budget and its memory of what it already checked.

    Held by the lane — a voice session, or a chat client's slot in the runner — because
    that is the scope the budget means something in. Two questions in one conversation
    that are really the same question should cost one run, and the sixth time a subject
    comes up it should cost nothing at all.
    """

    def __init__(self, max_runs: int = DEFAULT_MAX_RUNS, max_sources: int = DEFAULT_MAX_SOURCES):
        self.max_runs = max_runs
        self.max_sources = max_sources
        self.runs = 0
        self.answers: Dict[str, Result] = {}
        # Questions currently being checked, so two turns about the same thing do not
        # start two runs and deliver two follow-ups.
        self.in_flight: set = set()

    @staticmethod
    def key(question: str) -> str:
        # The brief's own normalization, so this cache and the briefs on disk agree
        # about which questions are the same question.
        return skippy_brief.brief_id_for(question)

    def allows(self) -> bool:
        return self.runs < self.max_runs

    def recall(self, question: str) -> Optional[Result]:
        return self.answers.get(self.key(question))

    def remember(self, result: Result) -> None:
        self.answers[self.key(result.question)] = result


# ---------------------------------------------------------------------------
# Running the check
# ---------------------------------------------------------------------------

def cached_answer(question: str, briefs_root: Optional[str] = None) -> Optional[Result]:
    """An answer already on disk for this question, if it is still worth trusting.

    Checked before spending a run, and the reason the third time a subject comes up is
    free. A stale brief is deliberately not used: the whole point of the staleness mark
    is that an old answer about the web is the kind of wrong that gets believed.
    """
    root = briefs_root or skippy_paths.briefs_root()
    try:
        brief = skippy_brief.open_brief(root, question=question)
    except Exception:
        logger.warning("Could not open the brief for a cached answer.", exc_info=True)
        return None
    if brief.stale:
        return None
    answer = brief.read_answer()
    if not answer.strip():
        return None
    return Result(
        question=question,
        answer=answer,
        brief_id=brief.brief_id,
        sources=len(brief.source_files()),
        cached=True,
    )


async def check(
    question: str,
    conversation: Conversation,
    *,
    roots: Sequence[str] = (),
    briefs_root: Optional[str] = None,
    max_steps: Optional[int] = None,
) -> Result:
    """Answer one question from the web, spending a run only if it has to.

    Never raises: this is called from a background task with nobody waiting on it, so a
    failure has to come back as something the lane can say out loud.
    """
    question = " ".join(str(question or "").split())
    if not question:
        return Result(question="", error="no question")

    remembered = conversation.recall(question)
    if remembered is not None:
        return remembered

    on_disk = cached_answer(question, briefs_root)
    if on_disk is not None:
        conversation.remember(on_disk)
        return on_disk

    if not conversation.allows():
        return Result(
            question=question,
            error=(
                f"the research budget for this conversation is used up "
                f"({conversation.max_runs} run(s))"
            ),
        )

    # Imported here rather than at module scope: skippy_agent imports enough of the
    # runtime that a cycle through the lanes is easy to create by accident, and this
    # module is imported by both of them.
    import skippy_agent
    from skippy_sandbox import SandboxError

    try:
        sandbox = _sandbox_for(roots, briefs_root)
    except SandboxError as exc:
        return Result(question=question, error=f"no workspace to run in ({exc})")

    conversation.runs += 1
    conversation.in_flight.add(conversation.key(question))
    try:
        outcome = await skippy_agent.run_research(
            question,
            sandbox,
            briefs_root=briefs_root,
            max_steps=max_steps,
            research=skippy_research.ResearchSession(max_sources=conversation.max_sources),
        )
    except Exception as exc:
        logger.exception("Autonomous research on %r failed.", question)
        return Result(question=question, error=f"{type(exc).__name__}: {exc}")
    finally:
        conversation.in_flight.discard(conversation.key(question))

    result = Result(
        question=question,
        answer=outcome.answer,
        brief_id=outcome.brief_id,
        sources=outcome.sources,
    )
    if not result.answer:
        result.error = outcome.summary or "the research produced nothing"
    else:
        conversation.remember(result)
    return result


def _sandbox_for(roots: Sequence[str], briefs_root: Optional[str] = None):
    """A sandbox for a run that will not touch the filesystem.

    A research run is offered no filesystem tools at all, so this exists only because
    every agent run has a sandbox. The workspace roots are used when there are any, so
    that project memory keys to the project being discussed; with none configured — a
    conversation on a machine with no repositories set up, which must still work — it
    falls back to the briefs directory, which the run also cannot read.
    """
    from skippy_sandbox import Sandbox

    usable = [root for root in roots if root]
    if usable:
        return Sandbox(usable)
    root = briefs_root or skippy_paths.briefs_root()
    os.makedirs(root, exist_ok=True)
    return Sandbox([root])


def acknowledgment(decision: Decision, spoken: bool = False) -> str:
    """The system note that tells the persona it is checking something.

    A note rather than a canned line, because the acknowledgment has to sound like
    whichever Skippy is talking — the chat one and the voice one are different people —
    and because a fixed string said every time is a tic.
    """
    where = "out loud, in one short sentence" if spoken else "in a sentence"
    return (
        f"SYSTEM NOTE: you are checking this on the web right now, in the background: "
        f"\"{decision.question}\". Answer what you can from what you already know, say "
        f"{where} that you are verifying it, and do not invent the part you are "
        "checking. The result will arrive on its own and you will report it then."
    )


def report(result: Result, spoken: bool = False) -> str:
    """The follow-up, phrased for the lane it is going to."""
    if result.error:
        return (
            f"I went to check \"{result.question}\" and could not: {result.error}. "
            "Take what I said before as unverified."
        )
    if spoken:
        # Out loud, a wall of citations is unusable. The brief has them.
        return result.answer
    footer = f"\n\n_Checked against {result.sources} source(s); brief `{result.brief_id}`._"
    return result.answer + (footer if result.brief_id else "")
