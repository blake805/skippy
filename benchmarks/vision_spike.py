"""Is a local vision model good enough to be worth wiring in?

Vision is the one capability gap on this bench that nothing else on the roadmap closes:
a photograph of a board, a scope trace, a panel, a part with a number on it. The iPhone
client is already there, and `skippy_factory` already raised `ws_max_size` for
"base64-encoded photo attachments" — for a feature that was never built on either side.

Wiring it up properly is a phase of work: a role in the registry, multimodal message
content through `skippy_llm` and the transcript, a wire path, and a picker in two Swift
apps. All of that is wasted if the answers are not good enough, and the answers are
exactly what nobody can predict — a local VL model is decent at "what kind of connector
is this" and much weaker at the thing you actually want, which is reading a part number
off a chip in a phone photo under shop lighting.

So this asks the question first, with no changes to anything. It builds the multimodal
request by hand and posts it: `query_message` passes message content straight through to
the endpoint, so a list-shaped content needs no support added to find out whether the
model is any use.

A vision model needs `mlx-vlm`, not `mlx_lm`: the server the rest of Skippy talks to
does not load VL weights. `mlx_vlm.server` speaks the same OpenAI-compatible
`/v1/chat/completions`, which is why nothing else here has to change.

    pip install mlx-vlm
    python -m mlx_vlm.server --model mlx-community/Qwen3-VL-32B-Instruct-8bit --port 8084

    SKIPPY_VISION_URL=http://127.0.0.1:8084/v1/chat/completions \\
    SKIPPY_VISION_MODEL=mlx-community/Qwen3-VL-32B-Instruct-8bit \\
        python benchmarks/vision_spike.py bench-photos/*.jpg --repeats 2 \\
            --ask "What is this board, and what is the largest chip on it?" \\
            --ask "Read every part number you can make out. Say which you are unsure of."

The photographs have to be real ones off the bench, taken the way you would actually
take them: the phone in one hand, whatever light is over the machine, no staging. A
spike run against clean product shots answers a question nobody asked.

Two request shapes are tried, because mlx-vlm documents its own (`input_image` with a
path) alongside the OpenAI one (`image_url` with a base64 data URL) and it is not worth
anyone's afternoon to find out which by reading. The standard shape goes first, the
other is the fallback, and the script says which was accepted — that answer is also what
the real integration will need to know.

Deliberately no role in `skippy_llm` yet. A registry entry nothing reads is the mistake
that left an `AGENT_CODER_ROLE` in there for months, and the whole point of a spike is
that it can conclude "not yet" and leave no trace.

What to look for, in this order:

1. **Does it read small text?** Part numbers, silkscreen, a value on a scope. This is the
   highest-value case here and the one most likely to fail. Try the same photo cropped;
   if cropping fixes it, the answer is a tiling step rather than a bigger model.
2. **Does it hallucinate detail?** A confident wrong part number is worse than "I cannot
   read that", exactly as with a fabricated citation. Ask the same question twice and see
   whether the answer is stable.
3. **How slow is it?** A vision turn that takes ninety seconds is a different feature
   from one that takes eight, and it decides whether this belongs in the chat lane or as
   a background job with a follow-up, the way research runs work.
"""

import argparse
import asyncio
import base64
import mimetypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8084/v1/chat/completions"
DEFAULT_MODEL = "mlx-community/Qwen3-VL-32B-Instruct-8bit"

DEFAULT_QUESTIONS = [
    "What is in this picture? Two sentences.",
    "Read every piece of text, number or marking you can make out. List them, and say "
    "plainly which ones you are not sure about rather than guessing.",
]


def as_data_url(path: str) -> str:
    kind, _ = mimetypes.guess_type(path)
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{kind or 'image/jpeg'};base64,{encoded}"


def content_for(shape: str, image: str, question: str) -> list:
    """The two ways a server of this kind takes an image.

    `openai` is the standard: a base64 data URL inline. `mlx-vlm` is the one that
    project's own examples use, where the image is a path the server opens itself —
    cheaper for a large photo on the same machine, and not something an OpenAI client
    would ever send.
    """
    if shape == "mlx-vlm":
        return [
            {"type": "text", "text": question},
            {"type": "input_image", "image_url": os.path.abspath(image)},
        ]
    return [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": as_data_url(image)}},
    ]


async def ask(url: str, model: str, image: str, question: str, timeout: float,
              shapes=("openai", "mlx-vlm")) -> dict:
    """Ask once, trying each request shape until one is accepted."""
    problems = []
    for shape in shapes:
        payload = {
            "model": model,
            "temperature": 0.0,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": content_for(shape, image, question)}],
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "seconds": 0.0}
        elapsed = time.monotonic() - started

        if response.status_code == 200:
            body = response.json()
            return {
                "answer": (body["choices"][0]["message"].get("content") or "").strip(),
                "seconds": elapsed,
                "shape": shape,
            }
        problems.append(f"{shape}: HTTP {response.status_code} {response.text[:200]}")
        # Only a rejected request is worth retrying in another shape. A 500 means the
        # server understood and fell over, and sending it something else would bury the
        # real error.
        if response.status_code >= 500:
            break
    return {"error": " | ".join(problems), "seconds": 0.0}


async def run(images, questions, url, model, repeats, timeout) -> int:
    for image in images:
        if not os.path.isfile(image):
            print(f"!! {image} does not exist")
            return 1
        size_kb = os.path.getsize(image) // 1024
        print(f"\n=== {image} ({size_kb} KB) ===")
        for question in questions:
            print(f"\n  Q: {question}")
            for index in range(repeats):
                result = await ask(url, model, image, question, timeout)
                if "error" in result:
                    print(f"  !! {result['error']}")
                    return 1
                label = f"  A{index + 1}" if repeats > 1 else "  A"
                print(
                    f"{label} ({result['seconds']:.1f}s, {result['shape']} format): "
                    f"{result['answer']}"
                )
    print(
        "\nAsk yourself: did it read the small text, did it invent any of it, and was it "
        "fast enough to sit in a conversation? Those three answers decide whether this "
        "gets built, and in which lane."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", nargs="+", help="photographs to try")
    parser.add_argument("--ask", action="append", dest="questions",
                        help="a question to put to each image (repeatable)")
    parser.add_argument("--repeats", type=int, default=1,
                        help="ask each question N times; instability is the tell for invention")
    parser.add_argument("--url", default=os.environ.get("SKIPPY_VISION_URL", DEFAULT_URL))
    parser.add_argument("--model", default=os.environ.get("SKIPPY_VISION_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    print(f"{args.model} at {args.url}")
    return asyncio.run(run(
        args.images, args.questions or DEFAULT_QUESTIONS,
        args.url, args.model, args.repeats, args.timeout,
    ))


if __name__ == "__main__":  # pragma: no cover - a spike, run by hand
    raise SystemExit(main())
