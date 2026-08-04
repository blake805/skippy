"""Does the RE lane still need the 480B, now that output arrives pre-digested?

ADR 0007 chose the 480B for the planner role on tool discipline. ADR 0018 changed the
premise it was chosen under: the model no longer reads tool-sized regions, it reads one
function at a time. So the question is open again, and this measures it instead of
arguing it — the same RE task, the same target, the same tools, on each model.

The target is benchmarks/updater.c: a firmware update path that checks a CRC and no
signature, with a provisioning key left in the binary. The answers are known in
advance, so a run can be scored rather than admired.
"""

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skippy_agent
import skippy_re
from skippy_sandbox import Sandbox

TASK = (
    "This is the firmware update path from one of our own products. Work out how an "
    "image is validated before it is applied, and record what you find. If the "
    "validation can be defeated, that is a weakness worth recording with a severity."
)


async def run_one(role: str, target: str, notes_root: str, workspace: str, steps: int):
    started = time.time()
    outcome = await skippy_agent.run_task(
        TASK,
        Sandbox([workspace]),
        mode="re",
        target=target,
        notes_root=os.path.join(notes_root, role),
        role=role,
        max_steps=steps,
        remember=False,
    )
    elapsed = time.time() - started

    pack = skippy_re.open_pack(os.path.join(notes_root, role), target=target)
    findings = []
    for path in pack.finding_files():
        text = open(path, encoding="utf-8").read()
        front = {}
        if text.startswith("---"):
            body = text.split("---", 2)[1]
            for line in body.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    front[key.strip()] = value.strip().strip('"')
        findings.append(front)

    return {
        "role": role,
        "status": outcome.status,
        "steps": outcome.steps,
        "tool_calls": outcome.tool_calls,
        "findings": outcome.findings,
        "commands_logged": outcome.commands_logged,
        "seconds": round(elapsed, 1),
        "summary": outcome.summary,
        "kinds": sorted(f.get("kind", "?") for f in findings),
        "weaknesses": [
            {"title": f.get("title"), "severity": f.get("severity"),
             "confidence": f.get("confidence")}
            for f in findings if f.get("kind") == "weakness"
        ],
        "titles": [f.get("title") for f in findings],
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--steps", type=int, default=16)
    parser.add_argument("--roles", default="fast,heavy")
    args = parser.parse_args()

    results = []
    for role in args.roles.split(","):
        print(f"=== {role} ===", flush=True)
        try:
            result = await run_one(
                role.strip(), args.target, args.notes, args.workspace, args.steps
            )
        except Exception as exc:  # a failed run is a result too
            result = {"role": role, "status": "crashed", "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)

    print("\n=== comparison ===", flush=True)
    print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
