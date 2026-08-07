"""How fast does a role actually generate, and does a draft model make it faster?

ADR 0007 measured prefill and decode once, to choose the models. This measures the
thing that decides how a session *feels*: the heavy role decodes at roughly 13.5 tok/s,
so a two-hundred-token step is fifteen seconds of watching a cursor, and an agent run is
dozens of steps. More of the gap between this and a hosted frontier model is latency
than is reasoning, and latency is the part that can be bought back with configuration
rather than weights.

Speculative decoding is the specific bet. A small model drafts several tokens, the large
one verifies them in a single forward pass, and every accepted token is one the large
model did not have to generate serially. It is exact — the output distribution is the
large model's, not the draft's, so this costs nothing in quality when it works. What
varies wildly is the acceptance rate, and for a 4-bit MoE with a different-architecture
draft it could be anywhere. That is the open question, and the reason this script exists
rather than a paragraph asserting a speedup.

    # baseline
    HF_HUB_OFFLINE=1 mlx_lm.server --model mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit \\
        --port 8081 --host 127.0.0.1
    python benchmarks/decode_speed.py --role heavy --label baseline --save

    # with the 30B drafting for it
    HF_HUB_OFFLINE=1 mlx_lm.server --model mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit \\
        --draft-model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --num-draft-tokens 4 \\
        --port 8081 --host 127.0.0.1
    python benchmarks/decode_speed.py --role heavy --label draft-30b --save

    python benchmarks/decode_speed.py --compare

Both processes have to fit in memory at once, which on 512GB is the whole point of
asking: the 480B at 4-bit is around 240GB and the 30B around 17GB, so the draft is
nearly free in space. Check `--num-draft-tokens`: too few and there is nothing to gain,
too many and the rejected tail is wasted verification.

Three prompt shapes, because a single number hides the trade. Prefill dominates a long
agent transcript, decode dominates a chat reply, and speculative decoding helps only the
second — a result showing a large decode speedup and a slightly *worse* warm prefill is
a real and expected outcome, not a broken run.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skippy_llm  # noqa: E402

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decode")

# Deliberately three shapes rather than one average.
#
# `chat` is a short prompt and a long answer: almost pure decode, which is where a draft
# model can help most and where the voice and chat lanes live.
# `agent_step` is a long prompt and a short answer, which is what every step of an agent
# run actually looks like — and it is mostly prefill, so a draft model may do nothing for
# it.
# `patch` is the case that must not regress: generating code. Speculative decoding is
# exact, so quality cannot change, but acceptance on structured output is its own
# question.
SHAPES = {
    "chat": {
        "prompt": (
            "Explain, in about three hundred words and in plain prose, why prompt "
            "caching makes an append-only transcript worth enforcing in an agent loop."
        ),
        "context": 0,
        "max_tokens": 400,
    },
    "agent_step": {
        "prompt": (
            "Given the file above, name the single function that should change and say "
            "why, in two sentences."
        ),
        # Padded to roughly a real transcript. The pad is repetitive on purpose: it is
        # measuring prefill throughput, not comprehension.
        "context": 12_000,
        "max_tokens": 80,
    },
    "patch": {
        "prompt": (
            "Write a Python function `chip_load(feed_rate, rpm, teeth)` returning inches "
            "per tooth, with a docstring and three pytest tests. Code only."
        ),
        "context": 0,
        "max_tokens": 350,
    },
}

PAD_LINE = "def helper_{n}(value: float) -> float:\n    return value * {n}.0\n\n"


def padding(target_chars: int) -> str:
    if target_chars <= 0:
        return ""
    body = []
    size = 0
    n = 0
    while size < target_chars:
        chunk = PAD_LINE.format(n=n)
        body.append(chunk)
        size += len(chunk)
        n += 1
    return "".join(body)


async def time_one(role: str, shape: dict) -> dict:
    context = padding(shape["context"])
    messages = []
    if context:
        messages.append({"role": "user", "content": f"Here is a module:\n\n{context}"})
    messages.append({"role": "user", "content": shape["prompt"]})

    started = time.monotonic()
    text = await skippy_llm.query_text(
        messages,
        role=role,
        temp=0.0,
        max_tokens=shape["max_tokens"],
        attempts=1,
        timeout=900.0,
    )
    elapsed = time.monotonic() - started

    # Characters over four is a rough token count and is honest about being one. The
    # server does report usage, but `query_text` drops it, and adding a second path
    # through skippy_llm for a benchmark would be the tail wagging the dog.
    approx_tokens = max(1, len(text) // 4)
    return {
        "seconds": round(elapsed, 2),
        "chars": len(text),
        "approx_tokens": approx_tokens,
        "approx_tok_per_s": round(approx_tokens / elapsed, 1) if elapsed else 0.0,
    }


async def measure(role: str, repeats: int, shapes: list) -> dict:
    out = {}
    for name in shapes:
        runs = []
        for index in range(repeats):
            print(f"  {name} {index + 1}/{repeats}", flush=True)
            runs.append(await time_one(role, SHAPES[name]))
        out[name] = {
            "runs": runs,
            "median_seconds": round(statistics.median(r["seconds"] for r in runs), 2),
            "median_tok_per_s": round(
                statistics.median(r["approx_tok_per_s"] for r in runs), 1
            ),
        }
    return out


def save(label: str, role: str, data: dict) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    endpoint = skippy_llm.endpoint(role)
    payload = {
        "label": label,
        "role": role,
        "model": endpoint.model,
        "url": endpoint.url,
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "shapes": data,
    }
    path = os.path.join(RESULTS, f"{label}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def load_all() -> list:
    try:
        names = sorted(n for n in os.listdir(RESULTS) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for name in names:
        with open(os.path.join(RESULTS, name), encoding="utf-8") as handle:
            out.append(json.load(handle))
    return out


def compare() -> str:
    boards = load_all()
    if len(boards) < 2:
        return "Need at least two saved runs to compare. Use --save with --label."

    baseline = next((b for b in boards if b["label"] == "baseline"), boards[0])
    lines = [f"Against '{baseline['label']}' ({baseline['model']}):", ""]
    for board in boards:
        if board is baseline:
            continue
        lines.append(f"{board['label']}:")
        for shape in SHAPES:
            was = (baseline["shapes"].get(shape) or {}).get("median_tok_per_s")
            now = (board["shapes"].get(shape) or {}).get("median_tok_per_s")
            if not was or not now:
                continue
            lines.append(
                f"  {shape:12} {was:6.1f} -> {now:6.1f} tok/s  ({now / was:.2f}x)"
            )
        lines.append("")
    lines.append(
        "A large gain on `chat` with little or none on `agent_step` is the expected "
        "shape: a draft model buys decode, and an agent step is mostly prefill."
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", default="heavy")
    parser.add_argument("--label", default="baseline", help="what this configuration is")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--shape", action="append", choices=sorted(SHAPES))
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--compare", action="store_true", help="print saved runs and exit")
    args = parser.parse_args(argv)

    if args.compare:
        print(compare())
        return 0

    shapes = args.shape or sorted(SHAPES)
    endpoint = skippy_llm.endpoint(args.role)
    print(f"{args.label}: role '{args.role}' -> {endpoint.model} at {endpoint.url}")

    data = asyncio.run(measure(args.role, args.repeats, shapes))
    print()
    for name, result in data.items():
        print(f"  {name:12} {result['median_seconds']:7.2f}s median  "
              f"~{result['median_tok_per_s']:6.1f} tok/s")
    if args.save:
        print(f"\nSaved {os.path.relpath(save(args.label, args.role, data))}")
    return 0


if __name__ == "__main__":  # pragma: no cover - a benchmark, run by hand
    raise SystemExit(main())
