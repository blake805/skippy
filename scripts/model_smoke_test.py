#!/usr/bin/env python3
"""Confirm every configured MLX role answers an OpenAI-compatible chat request.

    python3 scripts/model_smoke_test.py             # all roles
    python3 scripts/model_smoke_test.py --role heavy
    python3 scripts/model_smoke_test.py --code      # add a small codegen probe

Exit code is non-zero if any probed role fails, so this doubles as a Phase 1
gate before pointing the factory at new weights.
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skippy_llm  # noqa: E402

PING = [{"role": "user", "content": "Reply with exactly: SKIPPY_OK"}]
CODE = [
    {
        "role": "user",
        "content": (
            "Write a Python function `chunk(seq, n)` that yields successive n-sized "
            "lists from seq. Output only a python code block."
        ),
    }
]


async def probe(role: str, messages: list, label: str, temp: float) -> tuple[bool, str]:
    endpoint = skippy_llm.endpoint(role)
    started = time.monotonic()
    try:
        reply = await skippy_llm.query_model(
            messages, role=role, temp=temp, attempts=1, timeout=180.0, raise_on_error=True
        )
    except skippy_llm.ModelError as exc:
        return False, f"{exc}"
    elapsed = time.monotonic() - started
    preview = " ".join(reply.split())[:90]
    return True, f"{elapsed:6.2f}s  {endpoint.model}\n            {label}: {preview}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", action="append", choices=sorted(skippy_llm.MODELS))
    parser.add_argument("--code", action="store_true", help="also run a codegen probe")
    args = parser.parse_args()

    roles = args.role or ["fast", "heavy", "compressor"]
    failures = []

    for role in roles:
        endpoint = skippy_llm.endpoint(role)
        print(f"[{role}] {endpoint.url}")
        ok, detail = await probe(role, PING, "ping", 0.0)
        print(f"    {'ok  ' if ok else 'FAIL'}  {detail}")
        if not ok:
            failures.append(role)
            continue
        if args.code:
            ok, detail = await probe(role, CODE, "code", 0.1)
            print(f"    {'ok  ' if ok else 'FAIL'}  {detail}")
            if not ok:
                failures.append(role)

    if failures:
        print(f"\n{len(failures)} role(s) failed: {', '.join(sorted(set(failures)))}")
        return 1
    print(f"\nAll {len(roles)} role(s) responding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
