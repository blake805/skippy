"""Central registry for Skippy's local MLX inference endpoints.

Every model is addressed by *role* rather than by size, so swapping weights is a
config change instead of a code change:

    fast        :8080  triage, routing, compression-adjacent chores
    heavy       :8081  the coding brain (drives the agent loop end to end)
    compressor  :8082  squeezes large RAG/tool dumps before they hit a context window

All three are OpenAI-compatible `/v1/chat/completions` servers (`mlx_lm.server`
or LM Studio MLX), so only URLs and model names ever change.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("skippy_llm")

# Confirm the exact Hugging Face repo ids at download time and override via env;
# these defaults encode the intended fleet, not a guarantee that the id exists.
DEFAULT_FAST_MODEL = "mlx-community/Qwen3.6-35B-A3B-Instruct-4bit"
DEFAULT_HEAVY_MODEL = "avlp12/GLM-5.2-Alis-MLX-Dynamic-3.5bpw"
DEFAULT_COMPRESSOR_MODEL = "mlx-community/Qwen3.6-35B-A3B-Instruct-4bit"


class ModelError(RuntimeError):
    """Raised when an endpoint cannot be reached or returns an unusable body."""


@dataclass(frozen=True)
class ModelEndpoint:
    role: str
    url: str
    model: str
    max_tokens: int


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


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
    return {
        "fast": ModelEndpoint(
            role="fast",
            url=_env("SKIPPY_FAST_URL", "http://127.0.0.1:8080/v1/chat/completions"),
            model=_env("SKIPPY_FAST_MODEL", DEFAULT_FAST_MODEL),
            max_tokens=_env_int("SKIPPY_FAST_MAX_TOKENS", 4096),
        ),
        "heavy": ModelEndpoint(
            role="heavy",
            url=_env("SKIPPY_HEAVY_URL", "http://127.0.0.1:8081/v1/chat/completions"),
            model=_env("SKIPPY_HEAVY_MODEL", DEFAULT_HEAVY_MODEL),
            max_tokens=_env_int("SKIPPY_HEAVY_MAX_TOKENS", 16384),
        ),
        "compressor": ModelEndpoint(
            role="compressor",
            url=_env("SKIPPY_COMP_URL", "http://127.0.0.1:8082/v1/chat/completions"),
            model=_env("SKIPPY_COMP_MODEL", DEFAULT_COMPRESSOR_MODEL),
            max_tokens=_env_int("SKIPPY_COMP_MAX_TOKENS", 2048),
        ),
    }


MODELS: Dict[str, ModelEndpoint] = _build_registry()

# The role that plans and selects tools inside the agent loop. Defaults to the
# same role that writes code so the prompt cache stays warm across steps;
# set to "fast" to A/B a cheap-planner split.
AGENT_PLANNER_ROLE = _env("SKIPPY_AGENT_PLANNER_ROLE", "heavy")
AGENT_CODER_ROLE = _env("SKIPPY_AGENT_CODER_ROLE", "heavy")


def reload_registry() -> Dict[str, ModelEndpoint]:
    """Re-read the environment. Used by tests that point roles at a fake server."""
    MODELS.clear()
    MODELS.update(_build_registry())
    return MODELS


def endpoint(role: str) -> ModelEndpoint:
    try:
        return MODELS[role]
    except KeyError:
        raise ModelError(f"Unknown model role '{role}'. Known roles: {sorted(MODELS)}")


def endpoint_for_url(url: str) -> Optional[ModelEndpoint]:
    """Reverse lookup so legacy url-based call sites still get role defaults."""
    for candidate in MODELS.values():
        if candidate.url == url:
            return candidate
    return None


async def query_model(
    messages: List[dict],
    role: str = "fast",
    temp: float = 0.2,
    stop: Optional[List[str]] = None,
    max_tokens: Optional[int] = None,
    timeout: float = 600.0,
    attempts: int = 3,
    raise_on_error: bool = False,
) -> str:
    target = endpoint(role)
    payload = {
        "model": target.model,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tokens or target.max_tokens,
    }
    if stop:
        payload["stop"] = stop

    last_error = "no attempts made"
    async with httpx.AsyncClient() as client:
        for attempt in range(attempts):
            try:
                response = await client.post(target.url, json=payload, timeout=timeout)
                if response.status_code == 200:
                    body = response.json()
                    return body["choices"][0]["message"]["content"].strip()
                last_error = f"HTTP {response.status_code}: {response.text[:400]}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "%s endpoint attempt %d/%d failed: %s", role, attempt + 1, attempts, last_error
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(2.0 * (2**attempt))

    message = f"System Error: Failed to connect to MLX Server at {target.url}. ({last_error})"
    if raise_on_error:
        raise ModelError(message)
    return message


async def compress(text: str, instruction: str, word_budget: int = 400) -> str:
    """Squeeze an oversized tool result or RAG dump through the :8082 node."""
    prompt = (
        "You are a data extraction node. Extract ONLY what is needed to satisfy this "
        f"request:\n{instruction}\n\nRaw data:\n{text}\n\n"
        f"No conversational filler. Be dense and stay under {word_budget} words."
    )
    return await query_model(
        [{"role": "user", "content": prompt}], role="compressor", temp=0.1
    )
