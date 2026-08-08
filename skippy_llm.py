"""Everything Skippy needs to talk to a model.

Models are addressed by *role*, never by size or port, so changing weights is a
config change rather than a code change:

    fast        cheap turns, triage, routing
    heavy       the coding brain that drives the agent loop
    compressor  squeezes oversized tool output before it reaches a context window
    voice       the conversational brain behind /ws/voice
    reasoner    a frontier model consulted at hard decision points (coding mode)
    reasoner_re the same job for RE mode, expected to live on this machine

Every endpoint is an OpenAI-compatible ``/v1/chat/completions`` server, so a role
can point at a local ``mlx_lm.server`` or at a hosted API with no other change.
ADR 0007 covers the local/cloud policy this module enforces; ADR 0001 covers why
one role drives the whole agent loop.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("skippy_llm")

# Verified present in the local Hugging Face cache. `fast` and `compressor` share
# one model deliberately: Qwen2.5-Coder-32B used to hold the compressor role, but
# its 32K context window is smaller than a single objdump region, which would fail
# exactly when RE work needs compression most. Qwen3-Coder-30B-A3B has 256K.
DEFAULT_FAST_MODEL = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
DEFAULT_HEAVY_MODEL = "mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit"
DEFAULT_COMPRESSOR_MODEL = DEFAULT_FAST_MODEL
# The voice role gets a chat-tuned MoE rather than sharing the coder weights:
# same 3B active parameters (so the same speed class), but tuned for
# conversation — the coder model answering out loud was stilted and prone to
# the repetition loop skippy_voice now guards against. Small max_tokens on
# purpose: a spoken reply longer than this is a lecture.
DEFAULT_VOICE_MODEL = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# Cloud is opt-in and off by default. See ADR 0007: the point is not that cloud is
# forbidden, it is that reaching it is never an accident.
ALLOW_CLOUD_ENV = "SKIPPY_ALLOW_CLOUD"

# A second, separate gate for RE consults specifically. SKIPPY_ALLOW_CLOUD=1 says
# "this workspace may escalate to hosted models"; it does not say "the artifact I am
# reverse-engineering may leave this machine". Those are different decisions — RE
# payloads carry both a privacy cost and a safety-filter refusal risk that coding
# escalation does not — so relaxing the second one takes its own deliberate variable.
RE_ALLOW_CLOUD_ENV = "SKIPPY_RE_ALLOW_CLOUD"

# The cloud consult default. A hosted URL as a default is the one deliberate
# exception to "defaults are weights in the local cache" (ADR 0007): resolving it
# without SKIPPY_ALLOW_CLOUD raises CloudNotAllowed, so the default fails closed
# rather than confusingly. Fable 5 is the current SWE-bench/reasoning leader; treat
# it as a well-supported default and let the scoreboard arbitrate, not gospel.
DEFAULT_REASONER_URL = "https://api.anthropic.com/v1/chat/completions"
DEFAULT_REASONER_MODEL = "claude-fable-5"


class ModelError(RuntimeError):
    """An endpoint is unreachable, misconfigured, or returned an unusable body."""


class CloudNotAllowed(ModelError):
    """A role points off-machine while cloud escalation is disabled."""


@dataclass(frozen=True)
class ModelEndpoint:
    role: str
    url: str
    model: str
    max_tokens: int
    api_key: Optional[str] = None

    @property
    def is_local(self) -> bool:
        """True only for loopback.

        A LAN or tailnet address counts as non-local, which is deliberately
        conservative: the guarantee worth having is "the bytes did not leave this
        machine", and a tailnet peer is still another machine.
        """
        return (urlparse(self.url).hostname or "") in _LOOPBACK_HOSTS


def cloud_allowed() -> bool:
    return os.environ.get(ALLOW_CLOUD_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def re_cloud_allowed() -> bool:
    """Whether an RE-mode consult may reach an off-machine reasoner.

    Checked *in addition to* the global gate, by the consult path in the agent loop.
    The default answer is no even when SKIPPY_ALLOW_CLOUD is set, because a workspace
    that escalates coding questions to a hosted model has not thereby agreed to send
    firmware and disassembly there too.
    """
    return os.environ.get(RE_ALLOW_CLOUD_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using %d", name, raw, default)
        return default


def _build_registry() -> Dict[str, ModelEndpoint]:
    def build(role: str, prefix: str, port: int, model: str, max_tokens: int) -> ModelEndpoint:
        return ModelEndpoint(
            role=role,
            url=_env(f"{prefix}_URL", f"http://127.0.0.1:{port}/v1/chat/completions"),
            model=_env(f"{prefix}_MODEL", model),
            max_tokens=_env_int(f"{prefix}_MAX_TOKENS", max_tokens),
            api_key=os.environ.get(f"{prefix}_API_KEY", "").strip() or None,
        )

    return {
        "fast": build("fast", "SKIPPY_FAST", 8080, DEFAULT_FAST_MODEL, 4096),
        "heavy": build("heavy", "SKIPPY_HEAVY", 8081, DEFAULT_HEAVY_MODEL, 16384),
        # Shares port 8080 with `fast`. The heavy role is the one whose prompt cache
        # matters (it holds the growing agent transcript) and it has 8081 to itself;
        # neither fast nor compressor keeps a long shared prefix, so the contention
        # is not worth a third 16GB process. Point SKIPPY_COMP_URL at 8082 to split.
        "compressor": build("compressor", "SKIPPY_COMP", 8080, DEFAULT_COMPRESSOR_MODEL, 2048),
        # 250 tokens is roughly thirty seconds of speech — already the ceiling
        # of a tolerable spoken answer. At the earlier 600 the model regularly
        # produced lectures the persona prompt had already forbidden.
        "voice": build("voice", "SKIPPY_VOICE", 8083, DEFAULT_VOICE_MODEL, 250),
        # The consult roles (see the `consult` tool in skippy_agent). Coding mode's
        # reasoner defaults to a hosted frontier model — fails closed behind the
        # cloud gate until SKIPPY_ALLOW_CLOUD is set. RE mode's defaults to a local
        # port with NO default weights: no local thinking model has been chosen and
        # measured yet, and per ADR 0007 an aspirational repo id here would surface
        # as a confusing runtime failure. The consult tool refuses with instructions
        # while the model is unset. 16K tokens because a thinking model spends most
        # of its budget on the trace that precedes the answer.
        "reasoner": ModelEndpoint(
            role="reasoner",
            url=_env("SKIPPY_REASONER_URL", DEFAULT_REASONER_URL),
            model=_env("SKIPPY_REASONER_MODEL", DEFAULT_REASONER_MODEL),
            max_tokens=_env_int("SKIPPY_REASONER_MAX_TOKENS", 16384),
            api_key=os.environ.get("SKIPPY_REASONER_API_KEY", "").strip() or None,
        ),
        "reasoner_re": ModelEndpoint(
            role="reasoner_re",
            url=_env("SKIPPY_REASONER_RE_URL", "http://127.0.0.1:8082/v1/chat/completions"),
            model=os.environ.get("SKIPPY_REASONER_RE_MODEL", "").strip(),
            max_tokens=_env_int("SKIPPY_REASONER_RE_MAX_TOKENS", 16384),
            api_key=os.environ.get("SKIPPY_REASONER_RE_API_KEY", "").strip() or None,
        ),
    }


MODELS: Dict[str, ModelEndpoint] = _build_registry()

# The role that plans and picks tools. Defaults to the role that writes code so the
# prompt cache stays warm across steps (ADR 0001); set to "fast" to A/B a
# cheap-planner split without a refactor.
AGENT_PLANNER_ROLE = _env("SKIPPY_AGENT_PLANNER_ROLE", "heavy")

# There was an `AGENT_CODER_ROLE` here for a planner/coder split, and nothing ever read
# it. It is gone rather than left: ADR 0001 decided one role drives the whole loop, and
# splitting the writing off would need a second transcript and a second prompt cache —
# the exact cost that decision exists to avoid. A knob that does nothing is worse than no
# knob, because someone eventually sets it and expects an effect. Where a cheaper model
# genuinely does belong is a sub-run with a transcript of its own; that has its own
# setting, next to the thing it configures.


def reload_registry() -> Dict[str, ModelEndpoint]:
    """Re-read the environment. Used by tests that point roles at a fake server."""
    global AGENT_PLANNER_ROLE
    MODELS.clear()
    MODELS.update(_build_registry())
    AGENT_PLANNER_ROLE = _env("SKIPPY_AGENT_PLANNER_ROLE", "heavy")
    return MODELS


def endpoint(role: str) -> ModelEndpoint:
    """Resolve a role, refusing to reach off-machine unless that was asked for."""
    try:
        target = MODELS[role]
    except KeyError:
        raise ModelError(f"Unknown model role '{role}'. Known roles: {sorted(MODELS)}") from None

    if not target.is_local:
        if not cloud_allowed():
            raise CloudNotAllowed(
                f"Role '{role}' points at {target.url}, which is not on this machine, "
                f"and cloud escalation is off. Set {ALLOW_CLOUD_ENV}=1 to allow it."
            )
        # Never silent: if code or an RE target is leaving the machine, it is in the log.
        logger.warning(
            "CLOUD: role '%s' is served off-machine by %s at %s",
            role, target.model, urlparse(target.url).hostname,
        )
    return target


def describe_registry() -> str:
    """One line per role, for boot logs and the /health endpoint."""
    lines = []
    for role, target in MODELS.items():
        where = "local" if target.is_local else f"CLOUD ({urlparse(target.url).hostname})"
        lines.append(f"{role:11} {where:34} {target.model}")
    return "\n".join(lines)


# --- LEAKED TOOL CALL RECOVERY ---
def parse_leaked_tool_calls(content: str):
    """Recovers Qwen3-Coder XML-style tool calls that leaked into plain content.

    The model occasionally omits the opening <tool_call> frame token, so the
    server's state machine never enters tool-parsing mode and the raw
    <function=name><parameter=key>value</parameter></function> text lands in
    `content`. This parses those blocks into (tool_calls, cleaned_content).
    """
    calls = []
    for m in re.finditer(r'<function=([\w.-]+)>(.*?)</function>', content, re.DOTALL):
        name = m.group(1)
        args = {}
        for pm in re.finditer(r'<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>', m.group(2), re.DOTALL):
            value = pm.group(2).strip()
            # Structured values (arrays, objects, numbers, booleans) arrive as
            # JSON text; plain strings like "APPROVE" stay strings.
            try:
                args[pm.group(1)] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                args[pm.group(1)] = value
        calls.append({"id": str(uuid.uuid4()), "name": name, "arguments": args})
    if calls:
        content = re.sub(r'<function=[\w.-]+>.*?</function>', '', content, flags=re.DOTALL)
        content = content.replace("<tool_call>", "").replace("</tool_call>", "").strip()
    return calls, content


def _normalize_tool_calls(message: dict) -> List[dict]:
    """Flatten the server's tool_calls into {"id", "name", "arguments"(dict)}."""
    calls = []
    for tc in message.get("tool_calls") or []:
        func = tc.get("function", {})
        raw_args = func.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            args = {"_malformed_arguments": raw_args}
        calls.append({
            "id": tc.get("id", str(uuid.uuid4())),
            "name": func.get("name", ""),
            "arguments": args,
        })
    return calls


async def query_message(
    messages: Sequence[dict],
    role: str = "fast",
    temp: Optional[float] = 0.2,
    tools: Optional[List[dict]] = None,
    stop: Optional[List[str]] = None,
    max_tokens: Optional[int] = None,
    repetition_penalty: Optional[float] = None,
    timeout: float = 600.0,
    attempts: int = 3,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """Query a role and return the full assistant message.

    Returns {"content": str, "tool_calls": [{"id", "name", "arguments"(dict)}]}.
    Raises ModelError rather than returning an error string, so a dead endpoint
    can never be mistaken for something the model actually said.

    repetition_penalty ~1.05 stops the degenerate sentence-repetition loops prose
    roles fall into at low temperature. It MUST NOT be used when generating code:
    penalizing repeated tokens corrupts syntax, and regexes lose their closing
    parentheses.
    """
    target = endpoint(role)
    payload = {
        "model": target.model,
        "messages": list(messages),
        "max_tokens": max_tokens or target.max_tokens,
    }
    # None means "do not send the parameter at all", not "send a default". Some
    # hosted reasoning models (Claude Fable 5, measured live) reject a request that
    # names `temperature` even at a benign value, so a caller has to be able to stay
    # silent about it and take the server's own sampling.
    if temp is not None:
        payload["temperature"] = temp
    if stop:
        payload["stop"] = stop
    if tools:
        payload["tools"] = tools
    if repetition_penalty:
        payload["repetition_penalty"] = repetition_penalty
        # The server's default penalty window is 20 tokens — too short to catch
        # the sentence-length loops these models produce. Widen it.
        payload["repetition_context_size"] = 512

    headers = {"Authorization": f"Bearer {target.api_key}"} if target.api_key else None

    owned_client = client is None
    http = client or httpx.AsyncClient()
    last_error = "no attempts made"
    try:
        for attempt in range(attempts):
            disconnected = False
            try:
                response = await http.post(target.url, json=payload, headers=headers, timeout=timeout)
                if response.status_code == 200:
                    message = response.json()["choices"][0]["message"]
                    tool_calls = _normalize_tool_calls(message)
                    content = (message.get("content") or "").strip()
                    if tools and "<function=" in content:
                        leaked_calls, content = parse_leaked_tool_calls(content)
                        tool_calls.extend(leaked_calls)
                    return {"content": content, "tool_calls": tool_calls}
                last_error = f"HTTP {response.status_code}: {response.text[:400]}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # A connection that opened and then died without a response. This is
                # the shape a server-side generation crash arrives in — distinct from
                # a refused connection (server down) or an HTTP error (server spoke).
                disconnected = isinstance(
                    exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout)
                )
            logger.warning(
                "%s endpoint attempt %d/%d failed: %s", role, attempt + 1, attempts, last_error
            )
            if attempt + 1 < attempts:
                if disconnected and temp is not None:
                    # Warm the sampler, but only for the disconnect case: mlx_lm's
                    # Qwen3-Coder tool parser crashes its own handler thread on a tool
                    # call it cannot parse, which reaches us as a bare disconnect, and
                    # at temperature 0.1 against a warm prompt cache the retry
                    # regenerates near-identical text and dies identically. A refused
                    # connection or an HTTP error is not that: the payload was never
                    # the problem, and the caller should get the temperature it asked
                    # for. Logged, because a silently mutated sample contaminates
                    # anything that is measuring.
                    payload["temperature"] = min(temp + 0.15 * (attempt + 1), 1.0)
                    logger.warning(
                        "%s retry %d perturbs temperature %.2f -> %.2f (disconnect; an "
                        "identical payload would likely die identically)",
                        role, attempt + 2, temp, payload["temperature"],
                    )
                await asyncio.sleep(2.0 * (2 ** attempt))
    finally:
        if owned_client:
            await http.aclose()

    raise ModelError(f"Role '{role}' at {target.url} failed after {attempts} attempts. ({last_error})")


async def query_text(messages: Sequence[dict], role: str = "fast", **kwargs) -> str:
    """Text-only convenience wrapper. Drops any tool calls the model emitted."""
    return (await query_message(messages, role=role, **kwargs))["content"]


async def compress(text: str, instruction: str, word_budget: int = 400, **kwargs) -> str:
    """Squeeze an oversized tool result or retrieval dump through the compressor.

    This is the primary performance strategy, not a nicety: the heavy role
    prefills at ~200 tok/s, so every 2000 tokens of raw observation costs ~10s
    before it generates anything.
    """
    prompt = (
        "You are a data extraction node. Extract ONLY what is needed to satisfy this "
        f"request:\n{instruction}\n\nRaw data:\n{text}\n\n"
        f"No conversational filler. Be dense and stay under {word_budget} words."
    )
    return await query_text(
        [{"role": "user", "content": prompt}], role="compressor", temp=0.1, **kwargs
    )


def assistant_turn(message: dict) -> dict:
    """Rebuild an assistant message (with tool_calls) for the conversation history."""
    turn = {"role": "assistant"}
    if message["content"]:
        turn["content"] = message["content"]
    if message["tool_calls"]:
        turn["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
            }
            for tc in message["tool_calls"]
        ]
    return turn


class Transcript:
    """An append-only message list.

    mlx_lm.server caches prompts by prefix, which is worth roughly 20x on the
    heavy role: a 12K-token prefill measured 59.9s cold and 2.8-3.3s warm. Any
    edit to an already-sent message changes that prefix and forces a full
    re-prefill of everything after it.

    So this class has no way to delete or rewrite a turn. That is the point. The
    predecessor of this code trimmed history with `del messages[2:4]`, which
    silently cost a 60-second re-prefill every time a memory-management routine
    decided the transcript was too long.

    Shedding context is still sometimes necessary, but it has to be a deliberate
    act with a visible cost, which is what `fold` is for.
    """

    def __init__(self, system: Optional[str] = None):
        self._messages: List[dict] = []
        if system:
            self._messages.append({"role": "system", "content": system})

    def append(self, message: dict) -> None:
        self._messages.append(dict(message))

    def extend(self, messages: Sequence[dict]) -> None:
        for message in messages:
            self.append(message)

    @property
    def messages(self) -> List[dict]:
        """A copy. Mutating the result cannot corrupt the cached prefix."""
        return [dict(m) for m in self._messages]

    def __len__(self) -> int:
        return len(self._messages)

    def fold(self, keep_last: int, summary: str) -> "Transcript":
        """Return a NEW transcript: the system turn, a summary, then the last N turns.

        This invalidates the prompt cache by design — it rewrites the prefix. It
        returns a new object rather than mutating in place so that the cost is
        visible at the call site instead of hidden inside a helper.
        """
        if keep_last < 0:
            raise ValueError("keep_last must not be negative")

        folded = Transcript()
        head = self._messages[:1] if self._messages[:1] and self._messages[0]["role"] == "system" else []
        tail = self._messages[len(head):]
        kept = tail[-keep_last:] if keep_last else []
        dropped = len(tail) - len(kept)

        logger.warning(
            "Folding transcript: %d turns summarized, %d kept. This invalidates the "
            "prompt cache and the next step pays a full prefill.", dropped, len(kept),
        )
        # Copied, not aliased: every other path into _messages copies, and a shared
        # dict would become a footgun the moment anyone adds in-place mutation.
        folded._messages = [dict(m) for m in head]
        folded.append({"role": "user", "content": f"[EARLIER CONTEXT, SUMMARIZED]\n{summary}"})
        folded.extend(kept)
        return folded
