"""The agent loop: think, call tools, observe, repeat.

One role drives the whole loop (ADR 0001). The transcript only ever grows, because
mlx_lm.server caches prompts by prefix and rewriting any earlier message forces a
full re-prefill of everything after it — measured at roughly 60s against 3s on the
heavy role. `skippy_llm.Transcript` enforces that; this module's job is to never
work around it.

Native tool calling, not fenced JSON. That means the loop owes the server a `tool`
message for *every* tool call in an assistant message, including the ones it decides
not to run. An assistant turn with three tool calls and two answers is a malformed
transcript, and the failure surfaces later as confused output rather than an error.

The loop stops for one of four reasons, and says which: the model called `finish`,
it ran out of steps, it stopped calling tools, or it was cancelled. "Ran out of
steps" is never reported as success, however much was accomplished.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import prompts
import skippy_brief
import skippy_device
import skippy_dispatch
import skippy_exec
import skippy_fs
import skippy_llm
import skippy_memory
import skippy_paths
import skippy_re
import skippy_research
import tool_schemas
from skippy_sandbox import Sandbox, SandboxError, ToolResult, cap_text

logger = logging.getLogger("skippy_agent")

DEFAULT_MAX_STEPS = 40
HARD_MAX_STEPS = 200

# Research runs get a much shorter default than coding runs, and the reason is not
# cost. A question that forty steps of searching has not answered is a question the
# searching is not going to answer, and a run that keeps going past that produces a
# longer answer rather than a better one. Fourteen is comfortably enough for a plan,
# two or three searches and five or six pages.
DEFAULT_RESEARCH_STEPS = 14

# A sub-run reading the code to answer one question. Short on purpose: the value of the
# mechanism is that an expensive question is repaid to the caller as a paragraph, and a
# reader given forty steps produces an essay and spends the budget of the run that asked.
SUBAGENT_MAX_STEPS = 12
# How many a single run may spawn. A budget that can spawn things which have budgets is
# not a budget, and four is enough to answer the questions a real task raises without
# turning the run into a manager.
SUBAGENT_LIMIT = 4

# Which model reads. Empty means the same one driving the run. This is the one place a
# cheaper model can be swapped in without arguing with ADR 0001: a sub-run has a
# transcript of its own, so it has a prompt cache of its own, and using a different role
# here costs the parent's cache nothing. Whether the 30B is good enough at "find the
# thing and cite it" is a question for the scoreboard rather than for taste, so the
# default stays honest and the knob exists to measure it.
SUBAGENT_ROLE = os.environ.get("SKIPPY_SUBAGENT_ROLE", "").strip()

# How many times a run may put a question to the reasoner (the `consult` tool). Half
# the investigate limit, on purpose: a consult can cost real money per call when the
# role is hosted, and the failure mode being guarded is a loop that learns "when
# stuck, consult" and turns every hesitation into an escalation. Two is enough for a
# genuinely hard task to ask its big question and one follow-up.
CONSULT_LIMIT = 2
# The reasoner sees only what the consult sends, so attached files arrive whole — but
# a bound has to exist, and this one is deliberately generous (a consult is rare and
# its context is its whole value). cap_text keeps head and tail when it trims.
CONSULT_CONTEXT_CHARS = 120_000

# Which registry role answers a consult, by mode. RE consults resolve to a role that
# is expected to live on this machine; the enforcement (refusing an off-machine
# reasoner_re unless SKIPPY_RE_ALLOW_CLOUD is set on top of the global gate) lives in
# `_consult`, because the loop is the only place that knows which mode is asking.
CONSULT_ROLES = {"re": "reasoner_re"}
DEFAULT_CONSULT_ROLE = "reasoner"

# Above this, an observation goes through the compressor first. The heavy role
# prefills at ~200 tok/s, so 8000 characters of raw tool output costs about ten
# seconds of latency on every subsequent step, not just the one that produced it.
COMPRESS_THRESHOLD = 8_000

# Folding is expensive (it invalidates the prompt cache), so this is generous
# enough that a normal task never reaches it.
MAX_TRANSCRIPT_CHARS = 220_000
FOLD_KEEP_LAST = 8

# Two consecutive turns with no tool call means the model has stopped working. One
# nudge, because the first is usually the model narrating instead of acting and it
# recovers; a second means it believes it is done and is not going to call finish.
NUDGE_LIMIT = 2

# Changes to these cannot break a test suite, so finishing without having run one is not
# a guess about them. Kept deliberately short: prose only. A `.json` or a `.toml` is
# configuration that absolutely can break a build, and treating documentation as the
# exception rather than code as the rule is what keeps this from quietly widening.
PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

# How many times a `finish` may be sent back for want of evidence that the edits work.
# Exactly one. The prompt already says "a change you have not executed is a guess", and
# a single push-back turns that from advice into something the loop checks — but a run
# that genuinely cannot verify (no test suite, a broken toolchain, a task that is
# blocked) has to be able to end, and a run that cannot end is worse than one that ends
# unverified. The second finish goes through, and the model has been made to say why.
FINISH_PUSHBACK_LIMIT = 1

# Identical call this many times in the recent window and the loop intervenes.
REPEAT_LIMIT = 3
REPEAT_WINDOW = 6

# Inspection commands run since the last recorded finding before the loop says
# something. The prompt already asks for record-as-you-go and the first live RE run
# ignored it, batching five findings into the last five steps of eighteen; a count the
# model can see is a different message from an instruction it has already read past.
RE_RECORD_NUDGE_AFTER = 6

# RE tools whose output is evidence about the artifact, so the loop logs it to the pack
# and counts it towards the recording nudge. `list_symbols` is navigation rather than
# evidence and is deliberately absent: logging it would pad the record without adding to
# it, and a session that only listed symbols has not established anything to record.
RE_INSPECTION_TOOLS = ("disassemble_function", "decompile", "extract_artifact")

# Pages read since the last recorded claim before the loop says something. Same
# mechanism as the RE recording nudge and the same reason: the prompt already asks for
# record-as-you-go, and a count the model can see is a different message from an
# instruction it read past ten steps ago.
RESEARCH_RECORD_NUDGE_AFTER = 4

FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Ends the task. Call this when the work is done, or when you are blocked "
            "and cannot continue. Always call it rather than just describing what you did."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you changed and why, or what is blocking you.",
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths you modified, if any.",
                },
            },
            "required": ["summary"],
        },
    },
}


_HISTORY_ROLES = frozenset({"user", "assistant"})


def _clean_history(history: Optional[Sequence[dict]]) -> List[dict]:
    """Keep only well-formed user/assistant turns from a client-supplied history.

    The history arrives over the wire from whatever client is connected, so it is
    validated rather than trusted: a system turn here would fight the mode's own
    system prompt, tool-call turns would arrive without their matching results and
    break the transcript, and anything without text content is noise. Bad turns are
    dropped, not raised on — a malformed history should degrade to a fresh run, not
    fail one.
    """
    cleaned: List[dict] = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role in _HISTORY_ROLES and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content})
    return cleaned


@dataclass
class AgentOutcome:
    status: str  # finished | max_steps | stopped_without_finish | cancelled | failed
    summary: str = ""
    steps: int = 0
    files_changed: List[str] = field(default_factory=list)
    tool_calls: int = 0
    # The RE equivalent of files_changed: what the run left behind. Zero for a coding
    # run, which has no pack.
    findings: int = 0
    pack_id: str = ""
    # Commands the loop logged into the pack. Reported separately from findings
    # because it is the part that survives a run dying mid-investigation, and a run
    # with evidence but no conclusions is a different thing from one with neither.
    commands_logged: int = 0
    # Work items raised from `weakness` findings, for a later coding session.
    work_items: List[str] = field(default_factory=list)
    # Where this run was written down, so a caller can point at it.
    session_id: str = ""
    # What a research run produced: the synthesized answer with its citations, the
    # brief holding the sources it was written from, and how many there were. Reported
    # separately from `summary` because the summary is the model's account of the run
    # and this is the product of it — a caller that speaks one of them aloud wants this.
    answer: str = ""
    brief_id: str = ""
    sources: int = 0

    @property
    def ok(self) -> bool:
        """Only an explicit finish counts. A run that exhausted its budget may well
        have done useful work, but reporting it as success would hide the fact that
        the model never decided it was done."""
        return self.status == "finished"


class Cancelled(Exception):
    """Raised internally when a run is cancelled between steps."""


class AgentLoop:
    def __init__(
        self,
        task: str,
        sandbox: Sandbox,
        *,
        max_steps: Optional[int] = None,
        emit: Optional[Callable[[dict], Awaitable[None]]] = None,
        journal_dir: Optional[str] = None,
        role: Optional[str] = None,
        extra_context: str = "",
        mode: str = skippy_exec.DEFAULT_MODE,
        notes_root: Optional[str] = None,
        briefs_root: Optional[str] = None,
        target: str = "",
        memory: Optional[Any] = None,
        memory_root: Optional[str] = None,
        remember: bool = True,
        cursor: Optional[Any] = None,
        history: Optional[Sequence[dict]] = None,
        devices: Optional[Any] = None,
        approver: Optional[Any] = None,
        research: Optional[Any] = None,
    ):
        if not task or not str(task).strip():
            raise ValueError("An agent run needs a task.")

        self.mode = str(mode or skippy_exec.DEFAULT_MODE).lower()
        if self.mode not in skippy_exec.MODES:
            raise ValueError(
                f"Unknown mode {mode!r}. Known modes: {', '.join(sorted(skippy_exec.MODES))}."
            )

        self.task = str(task).strip()
        self.sandbox = sandbox
        # Only None means "use the default". An explicit 0 clamps to 1 rather than
        # falling through to 40 — this knob decides how much unattended editing
        # happens, so a caller passing 0 by mistake must not get a full run.
        # The budget belongs to the mode rather than to whoever starts one: a reader
        # given forty steps writes an essay, whatever the caller meant by leaving it
        # unset.
        default_steps = {
            "research": DEFAULT_RESEARCH_STEPS,
            "investigate": SUBAGENT_MAX_STEPS,
        }.get(self.mode, DEFAULT_MAX_STEPS)
        budget = default_steps if max_steps is None else int(max_steps)
        self.max_steps = max(1, min(budget, HARD_MAX_STEPS))
        self._emit = emit
        self.journal_dir = journal_dir
        self.role = role or skippy_llm.AGENT_PLANNER_ROLE

        self.step = 0
        self.tool_calls = 0
        self.files_changed: List[str] = []
        self._cancelled = False
        self._nudges = 0
        # Things the user has said since the run started, waiting for a step boundary.
        self._steering: List[str] = []
        self._investigations = 0
        self._consults = 0
        self._recent_calls: List[str] = []
        self._folds = 0
        self._commands_since_finding = 0
        self._pages_since_claim = 0
        self.work_items: List[str] = []
        self.answer = ""

        # Whether anything has been run since the last edit, and how it went. None means
        # "edits are outstanding and unexercised", which is the state `finish` is not
        # allowed to end on without saying something. Reset by every patch, answered by
        # every test or linter run.
        self._verified: Optional[bool] = None
        self._finish_pushbacks = 0
        # Files this run brought into existence, so that deleting them again can be
        # recognized as cleanup rather than a change. See the patch handling below.
        self._created_paths: set = set()
        # The command that last proved something green, kept so the run can leave behind
        # how this project is tested. Finding that out costs a session several steps of
        # guessing, and it is the same answer every time.
        self._verification_command = ""

        # RE mode gets a pack keyed by the target, so a second session on the same
        # artifact accumulates onto the first instead of starting over. The loop opens
        # it, never the model — see `open_pack`.
        self.notes_pack = None
        if self.mode == "re":
            root = notes_root or skippy_paths.notes_root()
            self.notes_pack = skippy_re.open_pack(
                root, target=target or "", title=self.task[:80]
            )

        # A research run gets a brief keyed by the question, for the same reason an RE
        # run gets a pack keyed by the target: the same question asked next month should
        # open what was already read rather than paying for it again. The loop opens it,
        # never the model.
        self.brief = None
        if self.mode == "research":
            self.brief = skippy_brief.open_brief(
                briefs_root or skippy_paths.briefs_root(), question=self.task
            )

        # The attached editor, when there is one. Held rather than consulted here: the
        # decision to route an edit through it belongs to the tool, so that the model
        # never sees a choice it could get wrong.
        self.cursor = cursor

        # The in-app approval gate for code edits, when one is bound to this run.
        # None means edits apply without a card (headless, or approvals off) — the
        # gate lives entirely in the tool layer, invisible to the model.
        self.approver = approver

        # Live hardware. Only RE mode opens a device service; coding mode must not
        # see these tools at all (re_tools vs workspace_tools), and a None here is
        # what makes a hallucinated device call fail closed in dispatch.
        self.devices = devices
        if self.devices is None and self.mode == "re":
            self.devices = skippy_device.DeviceService()

        # The web. Opened only for a research run: a coding or RE run has a target in
        # front of it and no business reaching the internet, and a None is what makes a
        # hallucinated web_search fail closed in dispatch the same way a device call
        # does.
        self.research = research
        if self.research is None and self.mode == "research":
            try:
                self.research = skippy_research.ResearchSession()
            except skippy_research.ResearchError:
                # A misconfigured backend must not stop the run at construction. The
                # tools then refuse with a message naming the variable to set, which is
                # something the model can report and a person can act on.
                logger.warning("No search backend; this run cannot reach the web.", exc_info=True)

        # Opened by the loop, keyed by the workspace roots, so that working on the same
        # repos tomorrow lands on the same memory without anyone naming it.
        self.memory = memory
        if self.memory is None and remember:
            try:
                self.memory = skippy_memory.open_project(
                    root=memory_root, workspace_roots=list(sandbox.roots)
                )
            except Exception:
                # An unmounted NAS must not stop a run; it only costs continuity.
                logger.warning("Project memory unavailable; running without it.", exc_info=True)

        system = {
            "re": prompts.RE_SYSTEM,
            "research": prompts.RESEARCH_SYSTEM,
            "investigate": prompts.INVESTIGATE_SYSTEM,
        }.get(self.mode, prompts.AGENT_SYSTEM)
        self.transcript = skippy_llm.Transcript(system=system)
        # Prior conversation turns from the client, seeded before the opening so the
        # model treats this run as a continuation. Kept ahead of the workspace/memory
        # opening because that block is scoped to *this* run (roots, note pack, the
        # task itself) and must remain the last thing the model reads.
        for turn in _clean_history(history):
            self.transcript.append(turn)
        if self.mode == "research":
            # No workspace roots here. A research run is offered no filesystem tools, and
            # listing repositories it cannot read would only invite it to try.
            opening = (
                f"You have {self.max_steps} steps for this question. Spend them on a few "
                "good sources rather than many shallow ones."
            )
        else:
            opening = "Workspace roots:\n" + "\n".join(
                f"- {sandbox.relative(root)} ({root})" for root in sandbox.roots
            )
        # Put in the opening message rather than left to a tool the model may call.
        # A tool it may call is a tool it mostly will not, and continuing prior work is
        # the whole point of keeping the record.
        if self.memory is not None:
            try:
                prior = self.memory.opening_context()
            except Exception:
                logger.warning("Could not read project memory.", exc_info=True)
                prior = ""
            if prior:
                opening += f"\n\n{prior}"
        if self.notes_pack is not None:
            findings = len(self.notes_pack.finding_files())
            opening += f"\n\nNote pack: {self.notes_pack.pack_id} ({findings} finding(s) so far)"
            if findings:
                # Said here rather than left to the prompt, because the single most
                # wasteful thing an RE session can do is re-derive last week's work.
                opening += ". Call read_notes before starting; this target has been looked at before."
            if self.notes_pack.target_changed:
                # In the opening message as well as on every read path. Findings about
                # bytes that have since changed are worse than no findings, and the
                # model has no way to notice on its own.
                opening += f"\n\nWARNING: {self.notes_pack.target_changed}"
        if self.brief is not None:
            sources = len(self.brief.source_files())
            claims = len(self.brief.claim_files())
            opening += (
                f"\n\nBrief: {self.brief.brief_id} ({sources} source(s), {claims} claim(s) "
                "so far)"
            )
            if sources or claims:
                # Said here rather than left to the prompt, for the same reason the note
                # pack announces itself: re-reading pages someone already read is the
                # most wasteful thing a research run can do, and the model has no way to
                # know the brief exists unless the loop says so.
                opening += (
                    ". This question has been researched before — call read_brief before "
                    "searching, and build on what is there."
                )
            if self.brief.stale:
                opening += f"\n\nWARNING: {self.brief.stale}"
        if extra_context:
            opening += f"\n\n{extra_context}"
        label = "Question" if self.mode == "research" else "Task"
        self.transcript.append({"role": "user", "content": f"{opening}\n\n{label}: {self.task}"})

    # -- plumbing ---------------------------------------------------------

    def cancel(self) -> None:
        """Ask the run to stop. Takes effect at the next step boundary, so a tool
        already running is allowed to finish rather than being torn down mid-write."""
        self._cancelled = True

    def steer(self, text: str) -> bool:
        """Say something to a run that is already going. Delivered at the next step.

        Until this existed the only thing you could say to a working agent was "stop",
        so watching one head down the wrong path for eight steps meant killing it and
        starting over — losing the good half of the work along with the bad. A sentence
        at the right moment is worth more than a better prompt at the start, because the
        wrong path is usually only visible once it has been taken.

        Delivered at a step boundary rather than injected immediately, for the reason
        every other interruption here works that way: a tool that is midway through
        writing files finishes first. And appended as an ordinary user turn, which is
        the whole trick — the transcript is append-only, so steering costs nothing and
        breaks no prompt cache, where editing the task in place would re-prefill
        everything.
        """
        text = str(text or "").strip()
        if not text:
            return False
        self._steering.append(text)
        return True

    async def emit(self, event: dict) -> None:
        if self._emit is None:
            return
        try:
            await self._emit(event)
        except Exception:
            # A dead websocket must not kill a run that is midway through editing
            # files. The run is the valuable thing; the UI can reconnect.
            logger.warning("Event sink failed; continuing without it.", exc_info=True)
            self._emit = None

    def tools(self) -> List[dict]:
        by_mode = {
            "re": tool_schemas.re_tools,
            "research": tool_schemas.research_tools,
            "investigate": tool_schemas.investigation_tools,
        }
        offered = by_mode.get(self.mode, tool_schemas.workspace_tools)()
        if not self._consult_available():
            # Withheld rather than offered-and-refused: a tool that can only say no
            # teaches the model to spend a step finding that out, and its absence is
            # also what makes the consult A/B a pure configuration split.
            offered = [t for t in offered if t["function"]["name"] != "consult"]
        return offered + [FINISH_SCHEMA]

    def _consult_role(self) -> str:
        return CONSULT_ROLES.get(self.mode, DEFAULT_CONSULT_ROLE)

    def _consult_available(self) -> bool:
        """Whether this run's consult could actually be answered under current policy.

        Mirrors the checks `_consult` enforces, so the tool is only on the menu when
        calling it could succeed: a reasoner with weights configured, and — when it
        is off-machine — both the global cloud gate and, for RE, the separate
        RE gate open.
        """
        target = skippy_llm.MODELS.get(self._consult_role())
        if target is None or not target.model:
            return False
        if target.is_local:
            return True
        if not skippy_llm.cloud_allowed():
            return False
        return self.mode != "re" or skippy_llm.re_cloud_allowed()

    # -- the loop ---------------------------------------------------------

    async def run(self) -> AgentOutcome:
        await self.emit({"type": "agent_start", "task": self.task, "max_steps": self.max_steps})
        try:
            outcome = await self._loop()
        except Cancelled:
            outcome = AgentOutcome(
                status="cancelled",
                summary=f"Cancelled after {self.step} step(s).",
                steps=self.step,
                files_changed=list(self.files_changed),
                tool_calls=self.tool_calls,
            )
        except skippy_llm.ModelError as exc:
            # Distinguished from a task failure: nothing is wrong with the task, the
            # model endpoint is unreachable or misconfigured.
            outcome = AgentOutcome(
                status="failed",
                summary=f"Model unavailable: {exc}",
                steps=self.step,
                files_changed=list(self.files_changed),
                tool_calls=self.tool_calls,
            )
        # Before the run is recorded, so what gets written down includes the answer.
        # Deliberately after every terminal outcome except the ones that never really
        # ran: a research run that used all its steps still read sources and recorded
        # claims, and refusing to write the answer because it never called finish would
        # throw away the entire run over a missing tool call.
        if self.brief is not None and outcome.status not in ("failed", "cancelled"):
            outcome.answer = await self._synthesize(outcome)

        # Written for every terminal outcome, including the ones that failed. A run
        # that ran out of steps halfway through a migration is the most useful thing
        # the next session could know, and a save-on-success rule would discard it.
        self._remember(outcome)

        await self.emit({
            "type": "agent_done",
            "status": outcome.status,
            "summary": outcome.summary,
            "steps": outcome.steps,
            "files_changed": outcome.files_changed,
            "answer": outcome.answer,
            "brief_id": outcome.brief_id,
            "sources": outcome.sources,
        })
        return outcome

    async def _synthesize(self, outcome: AgentOutcome) -> str:
        """Write the answer from the brief, in a call of its own.

        Separate from the loop that gathered the sources, and given only the claims and
        the citations rather than the transcript. The researching model ends its run
        holding twenty pages of untrusted page text and a strong pull toward whatever it
        read last; this call cannot be pulled anywhere, because the pages are not in
        front of it. It is also the reason a run that ran out of steps still produces
        something: the claims are on disk either way.
        """
        claims = self.brief.claims_block()
        sources = self.brief.citation_block()
        if not claims and not sources:
            # Nothing was read and nothing was recorded. Synthesizing here would produce
            # an answer from the model's own knowledge with a citation section under it,
            # which is the most convincing possible way to be wrong.
            return ""

        await self.emit({"type": "agent_synthesis", "brief_id": self.brief.brief_id})
        request = (
            f"Question: {self.task}\n\n"
            f"Claims recorded while researching it:\n{claims or '(none recorded)'}\n\n"
            f"Sources read:\n{sources or '(none)'}"
        )
        if outcome.status != "finished":
            # Said to the synthesis pass rather than hidden, because an answer written
            # from a run that stopped early must not read as a complete one.
            request += (
                f"\n\nNote: the research run ended as '{outcome.status}' rather than "
                "finishing, so this may be partial. Say what is missing."
            )
        if self.brief.stale:
            request += f"\n\nNote: {self.brief.stale}"

        try:
            answer = await skippy_llm.query_text(
                [
                    {"role": "system", "content": prompts.RESEARCH_SYNTHESIS},
                    {"role": "user", "content": request},
                ],
                role=self.role,
                temp=0.2,
                # Prose, not code: the penalty stops the sentence-repetition loops these
                # models fall into at low temperature. It must never be used where the
                # output is code, which is why it is set here and not in the loop.
                repetition_penalty=1.05,
            )
        except Exception:
            # The claims and the sources are already on disk, so a dead endpoint costs
            # the prose and nothing else. Better to hand back the record than to fail a
            # run that did all of its work.
            logger.warning("Synthesis failed; falling back to the brief.", exc_info=True)
            return (
                "The answer could not be written up (the model was unavailable), but the "
                f"research itself is in brief {self.brief.brief_id}:\n\n{claims}\n\n"
                f"Sources:\n{sources}"
            )

        answer = answer.strip()
        if not answer:
            return ""
        try:
            self.brief.write_answer(answer)
        except OSError:
            logger.warning("Could not write the answer into the brief.", exc_info=True)
        self.answer = answer
        return answer

    def _remember(self, outcome: AgentOutcome) -> None:
        if self.memory is None:
            return

        # Recorded whatever the outcome, because a run that ran out of steps still found
        # out how this project is tested, and that answer is the same next week. It is a
        # convention rather than a decision — a fact to reuse verbatim, not a judgment
        # with reasoning behind it — so it goes in the block every session opens with
        # instead of waiting to be recalled. Nothing asks the model for it: this is a
        # thing the loop watched happen.
        if self._verification_command:
            try:
                self.memory.learn_convention("test command", self._verification_command)
            except Exception:
                logger.warning("Could not record the test command.", exc_info=True)
        # `failed` and `cancelled` mean the run did not really happen — the endpoint was
        # unreachable, or someone stopped it. When such a run also produced nothing,
        # there is no history in it, and recording it actively hurts: a live run with a
        # dead endpoint wrote "Model unavailable: RemoteProtocolError ..." into project
        # memory as though it were something learned about the code, and in a context
        # this small that displaces real history.
        #
        # `max_steps` and `stopped_without_finish` are different: the model ran, called
        # tools and did not get there. That is worth knowing, whether or not it wrote
        # anything. And any run that touched the tree is kept regardless of how it
        # ended, because a half-applied edit is exactly what the next session must know.
        did_not_run = outcome.status in ("failed", "cancelled")
        # A logged command counts as having produced something: an RE run killed before
        # it concluded anything still left the evidence trail behind, and that is the
        # trail the next session picks up. A read source counts for the same reason.
        produced = (
            outcome.files_changed or outcome.findings or outcome.commands_logged
            or outcome.sources
        )
        if did_not_run and not produced:
            logger.info("Not recording a %s run that produced nothing.", outcome.status)
            return
        try:
            outcome.session_id = self.memory.record_session(
                task=self.task,
                status=outcome.status,
                summary=outcome.summary,
                files_changed=outcome.files_changed,
                findings=outcome.findings,
                steps=outcome.steps,
                mode=self.mode,
            )
        except Exception:
            # Losing the record of a run is bad; losing the run itself because
            # recording it failed would be worse. The work is already on disk.
            logger.warning("Could not record the session in project memory.", exc_info=True)

        # The answer goes into project memory as well as into the brief. The brief is
        # the working record, filed under a question nobody will think to look up; this
        # is what a later session — in any mode — finds by searching for the subject,
        # and it is what stops the same question being researched from scratch a third
        # time. Source URLs and the date travel with it, because an answer about the web
        # with neither is worth very little six months on.
        if self.brief is not None and outcome.answer:
            try:
                self.memory.add_research(
                    question=self.task,
                    answer=outcome.answer,
                    sources=[
                        {
                            "id": entry["front"].get("id", ""),
                            "url": entry["front"].get("final_url") or entry["front"].get("url", ""),
                            "title": entry["front"].get("title", ""),
                            "fetched": entry["front"].get("fetched", ""),
                        }
                        for entry in self.brief.sources()
                    ],
                    brief=self.brief.brief_id,
                )
            except Exception:
                logger.warning("Could not record the research in project memory.", exc_info=True)

    def _outcome(self, status: str, summary: str) -> AgentOutcome:
        return AgentOutcome(
            status=status,
            summary=summary,
            steps=self.step,
            files_changed=list(self.files_changed),
            tool_calls=self.tool_calls,
            # A brief's claims are its findings, counted the same way, so that a caller
            # that reports "N findings" needs to know nothing about which mode ran.
            findings=(
                len(self.notes_pack.finding_files()) if self.notes_pack
                else len(self.brief.claim_files()) if self.brief else 0
            ),
            pack_id=self.notes_pack.pack_id if self.notes_pack else "",
            commands_logged=len(self.notes_pack.command_files()) if self.notes_pack else 0,
            work_items=list(self.work_items),
            answer=self.answer,
            brief_id=self.brief.brief_id if self.brief else "",
            sources=len(self.brief.source_files()) if self.brief else 0,
        )

    async def _loop(self) -> AgentOutcome:
        while self.step < self.max_steps:
            if self._cancelled:
                raise Cancelled()

            self.step += 1
            await self._fold_if_needed()
            await self._deliver_steering()

            message = await skippy_llm.query_message(
                self.transcript.messages,
                role=self.role,
                temp=0.1,
                tools=self.tools(),
            )

            thought = message["content"]
            calls = message["tool_calls"]
            if thought:
                await self.emit({"type": "agent_thought", "step": self.step, "content": thought})

            # Appended before the tools run, so the transcript is in a valid state
            # even if a tool raises something unforeseen.
            self.transcript.append(skippy_llm.assistant_turn(message))

            if not calls:
                self._nudges += 1
                if self._nudges >= NUDGE_LIMIT:
                    return self._outcome(
                        "stopped_without_finish",
                        thought or "The model stopped calling tools without calling finish.",
                    )
                self.transcript.append({
                    "role": "user",
                    "content": (
                        "You did not call a tool. Continue working on the task by calling a "
                        "tool, or call finish if it is complete or you are blocked."
                    ),
                })
                continue

            self._nudges = 0
            finish: Optional[dict] = None

            for index, call in enumerate(calls):
                name, args = call["name"], call["arguments"]

                if name == "finish":
                    pushback = self._unearned_finish()
                    if pushback:
                        # Answered as a failed tool call and the loop continues, so the
                        # model gets the objection where it is holding the decision
                        # rather than as a note it reads past.
                        self._finish_pushbacks += 1
                        await self.emit({
                            "type": "agent_finish_refused",
                            "step": self.step,
                            "reason": pushback,
                        })
                        self._answer(call, ToolResult(False, pushback))
                        continue

                    finish = args
                    # Answered so the transcript stays valid; the loop exits below.
                    self._answer(call, ToolResult(True, "Run ended."))
                    # Anything the model asked for after finish is not run, but must
                    # still be answered or the assistant turn is left malformed.
                    for skipped in calls[index + 1:]:
                        self._answer(
                            skipped,
                            ToolResult(False, "Not executed: the run ended with finish."),
                        )
                    break

                result = await self._run_tool(call)
                observation = await self._observe(name, result)
                if self.notes_pack is not None:
                    note = self._record_re_artifacts(call, result)
                    if note:
                        observation = f"{observation}\n{note}"
                if self.brief is not None:
                    note = self._record_research_artifacts(call, result)
                    if note:
                        observation = f"{observation}\n{note}"
                self._answer(call, result, observation)

            if finish is not None:
                summary = str(finish.get("summary") or "").strip() or "Task reported complete."
                # Only a real list. The tool parser can hand an array-typed parameter
                # back as a raw string when its content failed to parse, and iterating
                # a string here would fill files_changed with single characters.
                reported = finish.get("files_changed")
                for path in reported if isinstance(reported, list) else []:
                    if isinstance(path, str) and path not in self.files_changed:
                        self.files_changed.append(path)
                return self._outcome("finished", summary)

            nudge = self._recording_nudge()
            if nudge:
                self.transcript.append({"role": "user", "content": nudge})

        # What survived the run is mode-specific, and reporting the wrong one makes a
        # productive run look empty: an RE run never changes a file, so "files changed:
        # none" is both true and actively misleading about work that is sitting on disk.
        if self.notes_pack is not None:
            kept = (
                f"Findings recorded: {len(self.notes_pack.finding_files())} in pack "
                f"{self.notes_pack.pack_id}, with "
                f"{len(self.notes_pack.command_files())} command(s) logged"
            )
        elif self.brief is not None:
            kept = (
                f"Claims recorded: {len(self.brief.claim_files())} in brief "
                f"{self.brief.brief_id}, from {len(self.brief.source_files())} source(s) read"
            )
        else:
            kept = f"Files changed: {', '.join(self.files_changed) or 'none'}"
        return self._outcome(
            "max_steps",
            f"Ran out of steps after {self.max_steps} without finishing. {kept}.",
        )

    async def _deliver_steering(self) -> None:
        """Hand the model anything the user said while it was working.

        Marked as arriving mid-run rather than passed off as part of the original task,
        because the difference matters: it is a correction to work already done, and a
        model that reads it as more of the brief tends to start again rather than adjust.
        """
        if not self._steering:
            return
        said, self._steering = self._steering, []
        for text in said:
            logger.info("Steering at step %d: %s", self.step, text)
            await self.emit({"type": "agent_steered", "step": self.step, "content": text})
            self.transcript.append({
                "role": "user",
                "content": (
                    "The user has just said this, while you are working. Take it as a "
                    f"correction to what you are doing now, not as a new task:\n\n{text}"
                ),
            })

    async def _run_tool(self, call: dict) -> ToolResult:
        name, args = call["name"], call["arguments"]
        self.tool_calls += 1
        await self.emit({
            "type": "agent_tool_call",
            "step": self.step,
            "call_id": call["id"],
            "tool": name,
            "args": redact(args),
        })

        if self._is_looping(name, args):
            result = ToolResult(
                False,
                f"You have called {name} with identical arguments {REPEAT_LIMIT} times without "
                "making progress. Change your approach, or call finish and explain what is "
                "blocking you.",
            )
        elif name == "investigate":
            # Handled here rather than in the dispatcher, for the same reason `finish`
            # is: what it spends is steps, and the loop is what owns the budget. It also
            # avoids dispatch having to import the loop that it is itself called from.
            result = await self._investigate(args)
        elif name == "consult":
            # Also loop-handled: it spends a capped per-run budget (and possibly
            # money), and the where-may-this-question-go policy depends on the mode,
            # which only the loop knows.
            result = await self._consult(args)
        else:
            result = await skippy_dispatch.dispatch(
                name, args, self.sandbox, journal_dir=self.journal_dir,
                mode=self.mode, notes_pack=self.notes_pack, memory=self.memory,
                cursor=self.cursor, devices=self.devices, approver=self.approver,
                research=self.research, brief=self.brief,
            )

        # Named rather than inferred from the shape of `data`. This used to key off the
        # presence of a `files` entry, which quietly assumed no other tool would ever use
        # that word — and one did, for a count rather than a list.
        if (
            name == "apply_patch"
            and result.ok
            and result.data.get("files")
            and not result.data.get("dry_run")
        ):
            reports = result.data["files"]
            for report in reports:
                path = report.get("path")
                if path and path not in self.files_changed:
                    self.files_changed.append(path)
            self._created_paths.update(
                r.get("path") for r in reports if r.get("action") == "create"
            )
            # Whatever was established by the last test run is no longer about this
            # tree. Reset rather than left, because "the tests passed" from before an
            # edit is the most misleading state a run can finish in. One exemption:
            # a patch that only deletes files this same run created is cleanup, not a
            # change — the tree the tests passed on is exactly the tree being handed
            # over, minus scratch. Without this, deleting a throwaway check script
            # re-arms the finish gate and the run re-verifies work it just verified.
            cleanup_only = all(
                r.get("action") == "delete" and r.get("path") in self._created_paths
                for r in reports
            )
            if cleanup_only:
                self._created_paths.difference_update(r.get("path") for r in reports)
            else:
                self._verified = None
            await self.emit({
                "type": "agent_patch",
                "step": self.step,
                "files": result.data["files"],
                "diff": result.data.get("diff", ""),
            })

        # Named by program rather than inferred from success, because a command that
        # exits 0 having done nothing about the change — `git diff`, a directory listing
        # — is not evidence, and treating it as such would make the check below
        # something a run discharges by accident.
        if name == "run_command" and skippy_exec.is_verification(result.data.get("command", "")):
            self._verified = bool(result.ok) and result.data.get("exit_code") == 0
            if self._verified:
                self._verification_command = str(result.data.get("command") or "")

        event = result.as_event()
        event.update({
            "type": "agent_tool_result",
            "step": self.step,
            "call_id": call["id"],
            "tool": name,
        })
        await self.emit(event)
        return result

    def _record_re_artifacts(self, call: dict, result: ToolResult) -> str:
        """Write down what an RE tool call produced, and say so where it matters.

        Both halves of this are here rather than in the tool because both must happen
        whatever the model does next. ADR 0013 states the rule and this is the third
        place it applies: anything that must happen has to be done by the loop.

        Returns a line to append to the observation, or "" — used only where the model
        needs to know something the tool itself could not tell it.
        """
        name = call["name"]

        if name == "run_command":
            # Only commands that actually ran. A rejected one has no output about the
            # target, and logging refusals would bury the evidence in noise.
            command = result.data.get("command")
            if not command:
                return ""
            self._commands_since_finding += 1
            try:
                self.notes_pack.log_command(
                    command=command,
                    output=result.content,
                    cwd=result.data.get("cwd", ""),
                    exit_code=result.data.get("exit_code"),
                    ok=result.ok,
                )
            except OSError:
                # The command already ran and the model already has its output. Losing
                # the log entry costs durability, not the investigation.
                logger.warning("Could not log a command to the note pack.", exc_info=True)
            return ""

        if name in RE_INSPECTION_TOOLS:
            # A disassembly is evidence exactly as much as an `objdump` region is, and
            # the reason for logging it is the same: it is what a later reader checks a
            # finding against. That these arrive through a structured tool rather than
            # `run_command` is an implementation detail of ADR 0018, and it would be a
            # poor reason for the record to have a hole in it.
            if not result.ok:
                return ""
            self._commands_since_finding += 1
            # Logged under a readable label rather than the full rizin invocation: the
            # label names the file and the pack index, and an absolute path plus a `-c`
            # script makes both unreadable. The exact invocation goes in the body, where
            # it is what a person retypes to check the finding.
            subject = str(
                result.data.get("symbol") or result.data.get("quarantine") or ""
            ).strip()
            label = f"{name} {subject}".strip()
            invocation = result.data.get("command") or ""
            body = f"$ {invocation}\n\n{result.content}" if invocation else result.content
            try:
                self.notes_pack.log_command(
                    command=label,
                    output=body,
                    exit_code=0,
                    ok=True,
                )
            except OSError:
                logger.warning("Could not log a tool result to the note pack.", exc_info=True)
            return ""

        if name != "note_finding" or not result.ok:
            return ""

        self._commands_since_finding = 0
        finding = result.data.get("finding") or {}
        if finding.get("kind") not in skippy_re.KINDS_REQUIRING_SEVERITY:
            return ""

        if self.memory is None:
            # Told to the model, because the finding is safely on disk but the thing
            # that makes it actionable — a later coding session seeing it — did not
            # happen, and its finish summary is then the only route to a human.
            return (
                "NOTE: project memory is unavailable, so this weakness was not raised as "
                "a work item for a later coding session. Repeat it in your finish summary."
            )

        args = call.get("arguments") or {}
        try:
            item = self.memory.add_work_item(
                title=str(args.get("title") or finding.get("id") or "").strip(),
                body=str(args.get("body") or "").strip(),
                severity=finding.get("severity", ""),
                confidence=finding.get("confidence", ""),
                pack=self.notes_pack.pack_id,
                finding=finding.get("id", ""),
                target=str(self.notes_pack.meta.get("target") or ""),
            )
        except OSError:
            logger.warning("Could not raise a work item for a weakness.", exc_info=True)
            return ""

        if item.get("id"):
            self.work_items.append(item["id"])
        return (
            f"Raised work item {item.get('id')} in project memory, so a later coding "
            "session on these repos will see this weakness without being told."
        )

    async def _investigate(self, args: dict) -> ToolResult:
        """Answer one question in a conversation of its own, and return only the answer.

        This is the context-management mechanism, not a delegation one. A question like
        "which callers depend on this signature" costs fifteen steps of reading, and
        those fifteen steps of file contents would sit in this run's transcript for the
        rest of its life — prefilled again on every subsequent step, folded eventually,
        and crowding out the task. Answering it somewhere else and keeping the paragraph
        is how a long run stays coherent.

        The child gets the same sandbox and reading tools, its own short budget, and no
        way to edit, run or spawn. It never records a session: a fragment of an
        investigation is not something a later session should open with, and the answer
        is already going where it is needed.
        """
        question = " ".join(str(args.get("question") or "").split())
        if not question:
            return ToolResult(False, "investigate needs a 'question'.")
        if self._investigations >= SUBAGENT_LIMIT:
            return ToolResult(
                False,
                f"You have used this run's {SUBAGENT_LIMIT} investigations. Read what you "
                "need directly with grep and read_file, or finish and say what is still "
                "unclear.",
            )
        self._investigations += 1

        where = str(args.get("where") or "").strip()
        opening = f"Start from {where}." if where else ""

        async def relay(event: dict) -> None:
            # Forwarded with a marker rather than swallowed, so the timeline shows the
            # reading happening instead of a silent gap on an expensive step, and so a
            # client can nest it under the call that caused it.
            await self.emit({**event, "sub": True, "parent_step": self.step})

        child = AgentLoop(
            question,
            self.sandbox,
            mode="investigate",
            max_steps=SUBAGENT_MAX_STEPS,
            emit=relay,
            role=SUBAGENT_ROLE or self.role,
            extra_context=opening,
            # No session record and no project memory: the child would otherwise write a
            # fragment into the history that a later run opens with, and read an opening
            # context it has no use for.
            remember=False,
            memory=None,
        )
        try:
            outcome = await child.run()
        except Exception as exc:
            logger.exception("Investigation of %r failed.", question)
            return ToolResult(False, f"The investigation failed: {type(exc).__name__}: {exc}")

        head = f"Investigated: {question}"
        if not outcome.ok:
            # Reported as a failure for the same reason the loop reports its own: a
            # reader that ran out of steps has not answered, and its last words read
            # exactly like an answer.
            return ToolResult(
                False,
                f"{head} — but the reader did not finish ({outcome.status}), so treat "
                "this as incomplete.",
                outcome.summary,
                {"question": question, "status": outcome.status, "steps": outcome.steps},
            )
        return ToolResult(
            True,
            f"{head} ({outcome.steps} step(s)).",
            outcome.summary,
            {"question": question, "status": outcome.status, "steps": outcome.steps},
        )

    async def _consult(self, args: dict) -> ToolResult:
        """Put one question to the mode's reasoner, and return its answer whole.

        Not a sub-run: `_investigate` spawns a child loop because the child needs
        tools to read with, and a consult has no tools at all. The parent packages
        the question and the files it names, the reasoner thinks once, and the
        answer comes back as a single observation. What transfers from investigate
        is the discipline around the call — a per-run cap, honest failure (a consult
        that errored is a failed ToolResult, never something that reads like an
        answer), and being handled by the loop because the loop owns budgets.

        This is also where the RE containment policy is enforced, not just
        configured: an RE run refuses an off-machine reasoner unless
        SKIPPY_RE_ALLOW_CLOUD is set on top of the global cloud gate, so pointing
        SKIPPY_REASONER_RE_URL at a hosted API by mistake fails loudly instead of
        quietly shipping a disassembly listing off the machine.
        """
        question = str(args.get("question") or "").strip()
        if not question:
            return ToolResult(False, "consult needs a 'question'.")
        if self._consults >= CONSULT_LIMIT:
            return ToolResult(
                False,
                f"You have used this run's {CONSULT_LIMIT} consults. Decide with what "
                "you have, or finish and say what is still unsettled.",
            )

        role = self._consult_role()
        target = skippy_llm.MODELS.get(role)
        if target is None or not target.model:
            # Normally unreachable (the tool is withheld when unconfigured), but the
            # message names the variable because the person reading it can act on it.
            return ToolResult(
                False,
                f"No consulting model is configured for this mode. Set "
                f"SKIPPY_{role.upper()}_MODEL (and _URL) to enable consults.",
            )
        if not target.is_local and self.mode == "re" and not skippy_llm.re_cloud_allowed():
            return ToolResult(
                False,
                f"Role '{role}' points off-machine at {target.url}, and RE material "
                f"never leaves this machine unless {skippy_llm.RE_ALLOW_CLOUD_ENV}=1 "
                "is set on top of the cloud gate. Point it at a local server, or "
                "proceed without a consult.",
            )

        # The loop reads the files rather than having the model paste code into the
        # call: tool-call arguments carrying source are the payload shape that used
        # to crash the parser, and file contents in an argument would also sit in
        # this transcript forever. `paths` may arrive as a bare string — the parser
        # fallback, or the model naming one file — and one file is a fine consult.
        paths = args.get("paths") or []
        if isinstance(paths, str):
            try:
                loaded = json.loads(paths)
                paths = loaded if isinstance(loaded, list) else [paths]
            except (json.JSONDecodeError, ValueError):
                paths = [paths]

        excerpts: List[str] = []
        attached: List[str] = []
        for path in paths:
            # Refused whole rather than sent partial: a reasoner answering without a
            # file the question turns on produces confident advice about code it
            # never saw, and nothing downstream can tell.
            try:
                read = skippy_fs.read_file(self.sandbox, str(path))
            except SandboxError as exc:
                return ToolResult(False, f"Could not attach {path}: {exc}")
            if not read.ok:
                return ToolResult(
                    False,
                    f"Could not attach {path}: {read.summary} Fix the path or drop "
                    "it, then consult again.",
                )
            display = read.data.get("path", str(path))
            attached.append(display)
            excerpts.append(f"--- {display} ---\n{read.content}")

        body = question
        if excerpts:
            body = (
                "Files the question turns on:\n\n"
                + cap_text("\n\n".join(excerpts), CONSULT_CONTEXT_CHARS)
                + f"\n\nQuestion:\n{question}"
            )

        self._consults += 1
        try:
            # temp=None on purpose: Fable 5's API rejects any request that names
            # `temperature` (measured live), and a local thinking server's own
            # default sampling is tuned for its traces. The reasoner samples its way.
            answer = await skippy_llm.query_text(
                [
                    {"role": "system", "content": prompts.CONSULT_SYSTEM},
                    {"role": "user", "content": body},
                ],
                role=role,
                temp=None,
            )
        except skippy_llm.ModelError as exc:
            # Includes CloudNotAllowed from the global gate. Reported as a failure
            # for the same reason a dead endpoint raises instead of returning prose:
            # nothing that comes back from this branch may read like an answer.
            return ToolResult(
                False,
                f"The consult failed: {exc} Treat this as no answer — decide with "
                "what you have, or finish and say what is blocked.",
            )

        if not answer.strip():
            return ToolResult(
                False,
                f"The consult returned nothing. Treat this as no answer; "
                f"{CONSULT_LIMIT - self._consults} consult(s) remain this run.",
            )
        return ToolResult(
            True,
            f"Consulted {target.model} ({len(attached)} file(s) attached). Its "
            "answer follows — advice from a model that saw only what you sent, "
            "so weigh it against the code.",
            answer,
            {"question": question, "role": role, "model": target.model, "paths": attached},
        )

    def _record_research_artifacts(self, call: dict, result: ToolResult) -> str:
        """Log every page a research run reads, and tell the model what to cite it as.

        The same rule as the RE command log, and the third place ADR 0013 applies:
        anything that must happen is done by the loop. A source recorded only when the
        model remembers to record it is a source that a run dying at step nine does not
        have — and here it is worse than that, because the citation check in
        `note_claim` can only refuse a fabricated URL if the real ones were logged
        mechanically.

        Returns a line for the observation. This one is load-bearing rather than
        informational: the id it reports is how the model cites the page it just read,
        and without it every claim would be refused.
        """
        if call["name"] == "note_claim" and result.ok:
            self._pages_since_claim = 0
            return ""
        if call["name"] != "web_fetch" or not result.ok:
            return ""

        data = result.data or {}
        url = str(data.get("final_url") or data.get("url") or "")
        if not url:
            return ""

        self._pages_since_claim += 1
        try:
            record = self.brief.log_source(
                url=str(data.get("url") or url),
                # What the page said, not what the model made of it. The observation was
                # capped and fenced for the model's benefit; the record keeps the text.
                text=result.content,
                title=str(data.get("title") or ""),
                final_url=url,
                chunk=int(data.get("chunk") or 1),
                chunks=int(data.get("chunks") or 1),
            )
        except OSError:
            # The page has already been read and the model already has it. Losing the
            # log entry costs the citation, not the reading — say so, because a claim
            # citing this page will now be refused and the model needs to know why.
            logger.warning("Could not log a source to the brief.", exc_info=True)
            return (
                "NOTE: this page could not be written to the brief, so you cannot cite it. "
                "Say what it told you in your finish summary instead."
            )

        if not record:
            return ""
        return (
            f"Logged as source {record['id']} in the brief. Cite it by that id in "
            "note_claim."
        )

    def _unearned_finish(self) -> str:
        """Why this `finish` is not accepted yet, or "" if it is.

        The prompt has always said "a change you have not executed is a guess", and a
        run could always ignore it: edit five files, run nothing, report success. This
        is ADR 0013's rule applied to the last step of a run — anything that must happen
        is done by the loop rather than asked of the model — and it is the same shape as
        the recording nudge, moved to the one moment where the claim is being made.

        Only edits to code trigger it. A run that changed nothing has nothing to have
        broken, a run that only touched documentation cannot have broken a test with it,
        and RE and research runs never reach this at all. Demanding a test run for a
        README edit would teach the model to run the suite as a ritual, which is the
        habit that makes a green tree stop meaning anything.
        """
        code = [
            path for path in self.files_changed
            if not str(path).lower().endswith(PROSE_SUFFIXES)
        ]
        if not code or self._verified is True:
            return ""
        if self._finish_pushbacks >= FINISH_PUSHBACK_LIMIT:
            # Said once. See FINISH_PUSHBACK_LIMIT: a run that cannot end is worse than
            # one that ends unverified, and by now the model has been asked and has
            # chosen to finish anyway, which is a position it can defend in its summary.
            return ""

        changed = ", ".join(code[:5])
        if self._verified is False:
            return (
                f"The last check you ran did not pass, and you have changed {changed}. "
                "Do not report this as done: fix what is failing, or call finish again "
                "and say plainly in your summary what is broken and why you are leaving "
                "it. Reporting a red tree as finished is the one outcome that wastes the "
                "next session's time completely."
            )
        return (
            f"You have changed {changed} but have not run anything since. A change you "
            "have not executed is a guess — run the project's tests, or whatever check "
            "this repository has, and finish once you know the result. If there is "
            "genuinely nothing to run here, call finish again and say so in your summary."
        )

    def _recording_nudge(self) -> str:
        """Say something when a run has been inspecting for a while and recording nothing.

        The command log means an early death no longer loses the *evidence*, but it
        cannot capture a conclusion, and the first live RE run established that the
        model batches findings at the end when left to itself. So this counts commands
        and quotes the number back: the prompt already asks for record-as-you-go, and
        the useful new information is how far past that the run has drifted.

        Resets on firing, so it recurs at the same interval rather than every step. A
        nudge repeated every step is one the model learns to read past.
        """
        if self.brief is not None:
            if self._pages_since_claim < RESEARCH_RECORD_NUDGE_AFTER:
                return ""
            count = self._pages_since_claim
            self._pages_since_claim = 0
            return (
                f"You have read {count} pages without recording a claim. The pages "
                "themselves are saved to the brief, so the sources are safe — but what you "
                "concluded from them is not, and the final answer is written from your "
                "claims rather than from this conversation. Record what these sources "
                "support now, at whatever confidence is honest, before reading anything "
                "further."
            )
        if self.notes_pack is None or self._commands_since_finding < RE_RECORD_NUDGE_AFTER:
            return ""
        count = self._commands_since_finding
        self._commands_since_finding = 0
        return (
            f"You have run {count} inspection commands without recording a finding. Each "
            "command and its output is already saved to the note pack, so the evidence is "
            "safe — but your conclusions are not, and a run that stops here leaves nobody "
            "anything to read. Record what you have established now, at whatever "
            "confidence is honest, before inspecting anything further. Use kind "
            "'question' for what you do not understand yet."
        )

    def _answer(self, call: dict, result: ToolResult, observation: Optional[str] = None) -> None:
        """Append the `tool` message that answers one call. Every call gets one."""
        self.transcript.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": observation if observation is not None else result.as_observation(),
        })

    async def _observe(self, name: str, result: ToolResult) -> str:
        """What the model actually sees, compressed if it is too big to be worth it."""
        raw = result.as_observation()
        if len(raw) <= COMPRESS_THRESHOLD:
            return raw

        await self.emit({"type": "agent_compress", "step": self.step, "tool": name,
                         "chars": len(raw)})
        try:
            condensed = await skippy_llm.compress(
                result.content,
                instruction=f"The agent ran `{name}` while working on: {self.task}",
            )
        except Exception:
            # Truncation is worse than compression but much better than failing the
            # step, and the model is told which it got.
            logger.warning("Compression failed; truncating instead.", exc_info=True)
            return cap_text(raw, COMPRESS_THRESHOLD) + "\n[truncated: compression unavailable]"

        head = ("OK: " if result.ok else "ERROR: ") + result.summary
        return f"{head}\n[compressed from {len(raw)} chars]\n{condensed}"

    def _is_looping(self, name: str, args: dict) -> bool:
        """True when the same call keeps coming back.

        Compared over a window rather than against only the previous call, because
        the common stuck pattern is alternating between two calls, which a
        compare-with-last check never catches.
        """
        signature = json.dumps({"t": name, "a": args}, sort_keys=True, default=str)
        self._recent_calls.append(signature)
        del self._recent_calls[:-REPEAT_WINDOW]
        return self._recent_calls.count(signature) >= REPEAT_LIMIT

    async def _fold_if_needed(self) -> None:
        """Compact the transcript when it gets too long, and say so.

        This is the one operation that breaks the prompt cache on purpose, so the
        next step pays a full prefill. It is loud for that reason.
        """
        size = sum(len(m.get("content") or "") for m in self.transcript.messages)
        if size <= MAX_TRANSCRIPT_CHARS:
            return

        await self.emit({"type": "agent_fold", "step": self.step, "chars": size})
        history = "\n\n".join(
            f"[{m.get('role')}] {m.get('content') or ''}" for m in self.transcript.messages
        )
        try:
            summary = await skippy_llm.compress(
                history, instruction=prompts.FOLD_SUMMARY, word_budget=800
            )
        except Exception:
            logger.warning("Fold summary failed; keeping the transcript as-is.", exc_info=True)
            return

        self.transcript = self.transcript.fold(FOLD_KEEP_LAST, summary)
        self._folds += 1


def redact(args: Optional[dict]) -> Dict[str, Any]:
    """Trim bulky arguments out of the event stream.

    The full diff is emitted separately by the patch event, so repeating whole file
    bodies here would only bloat the socket.
    """
    trimmed: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key == "edits" and isinstance(value, list):
            trimmed[key] = [
                {"path": e.get("path"), "action": e.get("action", "edit")}
                for e in value if isinstance(e, dict)
            ]
        elif isinstance(value, str) and len(value) > 600:
            trimmed[key] = value[:600] + f"... [+{len(value) - 600} chars]"
        else:
            trimmed[key] = value
    return trimmed


async def run_task(
    task: str,
    sandbox: Sandbox,
    *,
    max_steps: Optional[int] = None,
    emit: Optional[Callable[[dict], Awaitable[None]]] = None,
    journal_dir: Optional[str] = None,
    role: Optional[str] = None,
    mode: str = skippy_exec.DEFAULT_MODE,
    notes_root: Optional[str] = None,
    briefs_root: Optional[str] = None,
    target: str = "",
    memory: Optional[Any] = None,
    memory_root: Optional[str] = None,
    remember: bool = True,
    cursor: Optional[Any] = None,
    history: Optional[Sequence[dict]] = None,
    devices: Optional[Any] = None,
    approver: Optional[Any] = None,
    research: Optional[Any] = None,
) -> AgentOutcome:
    """Convenience entry point for one task."""
    loop = AgentLoop(
        task, sandbox, max_steps=max_steps, emit=emit, journal_dir=journal_dir,
        role=role, mode=mode, notes_root=notes_root, briefs_root=briefs_root,
        target=target, memory=memory, memory_root=memory_root, remember=remember,
        cursor=cursor, history=history, devices=devices, approver=approver,
        research=research,
    )
    return await loop.run()


async def run_research(question: str, sandbox: Sandbox, **kwargs) -> AgentOutcome:
    """Answer one question from the web, and leave a brief behind.

    A thin wrapper on purpose. Research is the same think-tool-observe-finish loop with
    a different toolset, a different prompt and a different record — making it a mode
    rather than a second loop is what keeps the transcript contract, the repeat
    detection, the folding and the cancellation shared. What is added on top is one
    call: the synthesis pass that turns the brief into an answer.
    """
    return await run_task(question, sandbox, mode="research", **kwargs)
