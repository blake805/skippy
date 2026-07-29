# ADR 0010 — The agent loop, on native tool calling

- **Status:** accepted
- **Date:** 2026-07-29
- **Amends:** ADR 0001, which described this loop before the runtime existed

## Context

Slices 4 and 5 gave Skippy tools. This is the loop that uses them: think, call
tools, observe, repeat, until the model says it is done or a budget stops it.

ADR 0001 chose one role to drive the whole loop, so the prompt cache stays warm
across steps. That still holds and this implements it. What ADR 0001 did not settle
is how tool calls travel, how the loop knows it is finished, and what happens when
the model gets stuck — which is most of what determines whether an unattended run
is useful or just expensive.

## Decision

**Native OpenAI-style tool calling**, not the fenced-JSON protocol lineage B used.
The server parses tool calls into structured fields, so there is no prompt asking
the model to "output ONLY this exact JSON", no regex for fenced blocks, and no
class of failure where prose wraps a valid call and the parse misses it.

The consequence is a contract the loop has to honour: **every tool call in an
assistant turn gets exactly one `tool` message in reply.** An assistant turn with
three calls and two answers is a malformed transcript, and it does not fail loudly —
it produces confused output several steps later. This includes calls the loop
deliberately does not run: when the model calls `finish` alongside other tools, the
rest are answered with "not executed" rather than dropped.

**The transcript only ever grows.** `skippy_llm.Transcript` enforces it and this
module's job is to not work around it. Lineage B trimmed history with
`del messages[2:4]`, which silently cost a full re-prefill — roughly 60s against 3s
on the heavy role — every time its memory management decided the transcript was
long. When compaction is genuinely needed, `fold` rewrites the prefix as one
deliberate, logged act, and the summary is produced by an *extraction* prompt
rather than a summarization one, because "summarize this" yields prose about the
session ("the agent explored the repository") instead of the file paths and failed
approaches the next step actually needs.

**Four stop reasons, and the loop says which:** `finished`, `max_steps`,
`stopped_without_finish`, `cancelled`. Only `finished` is success.

## Only an explicit finish counts as success

The tempting shortcut is to treat "files were changed" as success when the step
budget runs out. It is wrong. The model never decided the work was done, so
reporting success hides a stalled run behind a plausible summary, and the user
finds out by reading the diff later. `max_steps` reports what changed and is still
not `ok`.

`stopped_without_finish` exists for the model that stops calling tools and starts
narrating. One nudge first, because the first prose turn is usually narration and
the model recovers immediately; a second consecutive one means it believes it is
done and is not going to call `finish`.

## Getting stuck

Repeated-call detection compares against a **window** of recent calls, not just the
previous one. The realistic stuck pattern is alternating between two calls — read a
file, grep, read the same file, grep again — which a compare-with-last check never
catches. Tripping it produces an observation telling the model to change approach
or call `finish` and explain the obstacle, which is more useful than letting it
burn the remaining budget.

## Every tool failure is an observation

`skippy_dispatch.dispatch` never raises. An exception escaping a tool would strand
the run: the model never learns what went wrong, and the transcript keeps a call
with no answer. So a hallucinated tool name, bad arguments, a sandbox violation and
an internal crash all come back as `ToolResult(ok=False)` with a message written for
the model.

The unknown-tool message **lists the real tool names**. This was learned from
benchmarking: a bare "unknown tool" reply led the model to guess a second wrong
name, while naming the available ones made it recover on the next step.

**The sandbox and the journal directory are injected, never accepted from the
model.** A tool call that could choose its own roots would defeat the point of
having them, and one that could redirect its own pre-images could arrange for them
not to exist. Both are popped from the arguments. The journal case is subtle: the
loop overwrites it when a journal is configured, so the pop only matters when one
is not — which is exactly when a model-supplied path would be used unchallenged.
Mutation testing is what found that; the obvious test passed with the guard removed.

## Consequences

- Observations above ~8000 characters go through the compressor first. The heavy
  role prefills at ~200 tok/s, so raw tool output is paid for on every later step,
  not only the one that produced it. If compression fails the loop truncates and
  says so rather than failing the step.
- A failing event sink is logged once and then dropped. A disconnected UI must not
  kill a run that is midway through editing files; the run is the valuable thing
  and the UI can reconnect.
- Cancellation takes effect at a step boundary, so a tool already running is
  allowed to finish rather than being torn down mid-write.
- `max_steps=0` clamps to 1 rather than falling through to the default. This knob
  decides how much unattended editing happens, so a caller passing 0 by mistake
  must not get a full run.
- No session persistence yet, so a run starts cold. `extra_context` is the seam
  where prior-session memory will arrive.

## Verified against the real model

The loop was run end to end against `Qwen3-Coder-480B-A35B-Instruct-4bit` on a real
repository, twice, with the task "add `mm_to_thou`, export it, and add a test
following the existing conventions". Both runs finished in about 90 seconds, and
both produced correct work whose own test suite passes: the function added, the
export updated in *both* the import and `__all__`, and a test matching the
surrounding style.

Two things worth recording from watching it:

**The system prompt's batching instruction works.** The model explored with four
read-only calls, then put six edits across three files into a single `apply_patch`
call, which is exactly the behaviour that makes the all-or-nothing guarantee in ADR
0009 worth having.

**It tried to run the tests and could not.** On the second run it created a scratch
verification script at the repo root, then deleted it on the next step — it wanted
to execute something, found no tool for it, and cleaned up after itself. That is
the clearest available evidence for what the next slice should be: the loop can
write code but cannot check whether the code works, so its only verification is
re-reading what it just wrote.
