# 0011 — Command execution: an allowlist that admits what it is

Status: accepted
Date: 2026-07-29
Supersedes nothing. Follows 0010 (agent loop).

## Context

Through ADR 0010 the agent could read, search and write, but not execute. Its only
verification was re-reading what it had just written, which confirms the edit landed
and nothing about whether it works.

The live run at the end of 0010 made the gap concrete. At step 9 the model wrote a
scratch verification script to the repo root; at step 10, having found no tool that
could run it, it deleted the script and moved on. It knew what it needed and could
not get there.

## Decision

One tool, `run_command`, which runs a single allowlisted program without a shell.

### The allowlist is accident prevention, not containment

This is the part worth being exact about, because the natural reading is wrong and
an over-trusted boundary is worse than an acknowledged gap.

`pytest` imports `conftest.py` and executes its module body before collecting a
single test. The agent can write `conftest.py` with `apply_patch`. So "the agent may
run pytest" and "the agent may execute arbitrary code" are the same permission, and
no list of program names changes that. `npm test` runs package.json scripts,
`cargo test` compiles build.rs, `make test` runs whatever the Makefile says. This was
verified rather than assumed — a conftest.py whose module body writes a file, run
under an allowlisted `python -m pytest`, wrote the file.

What the allowlist actually buys is protection from plausible mistakes: a model that
has misread a situation cannot casually run `rm -rf`, `git push --force`,
`git reset --hard`, or `curl | sh`. Those are the realistic failures on a machine
running an agent against the user's own repositories, and they are worth preventing.

Two consequences follow.

**There is no arbitrary-shell tool, not even an approval-gated one.** Lineage B had
`run_terminal`, unrestricted, guarded by a human approval prompt. Asking permission
for the loud path while the quiet path stands open is theatre; it also spends the
user's attention on a prompt that appears often enough to earn a reflexive yes, and
the approval it relies on has the one-per-socket limitation documented in
`tests/test_hub.py`. A model that needs something outside the list says so in its
`finish` summary and a human runs it. That is a slower loop and an honest one.

**Real containment is not a change to this file.** It requires the whole run inside a
VM or container, which is a deployment decision. Recording that here so the next
person does not go looking for it in `skippy_exec.py`.

### What is guaranteed

Narrower than containment, and still worth having:

- **No shell.** `create_subprocess_exec` with a parsed argv, never `shell=True`.
  Operators are rejected with an explanation rather than passed through as literal
  arguments, because `pytest && rm -rf /` running pytest against a file called `rm`
  is safe but incomprehensible. The check is quote-aware: `pytest -k "slow; fast"` is
  a valid argument and is allowed, while `pytest; rm -rf /` is not — shlex splits the
  latter into `pytest;` as a single token, so the operator hides inside the program
  name and a whole-token check would miss it.
- **Programs by name only.** `./pytest`, `/bin/sh` and `/usr/bin/env python` are
  refused; a path would sidestep the list entirely.
- **Read-only git.** Subcommand allowlist plus a flag denylist, because `branch` and
  `tag` read by default and delete with `-d`. Writes are absent deliberately:
  `apply_patch` already provides atomic writes with a journal, and letting the agent
  commit or check out belongs with session checkpointing.
- **Bounded runtime**, with the whole process tree killed. `start_new_session=True`
  puts the child in its own process group so a timeout can kill what it spawned;
  killing only the direct child leaves node holding a port, and the next run fails
  confusingly.
- **Bounded output**, keeping the head and tail and dropping the middle. The tail
  carries the failure summary, which is the part that answers the question that was
  asked. The stream is drained to EOF even when discarding, because a full pipe
  blocks the child forever.
- **No secrets in the child environment.** Allowlisted, not filtered: a denylist of
  secret-looking names is a guessing game, and the code inheriting this environment
  is the repository's own test suite. When reverse-engineering someone else's
  project, that is not code to hand an API key.

### `python -c` is refused, `python script.py` is allowed

Not on danger grounds — `-c` is no more dangerous than `pytest`. The distinction is
auditability: a script on disk went through `apply_patch`, so it is journaled,
reviewable and re-runnable, while inline code leaves no trace of what executed.

The refusal message says this and tells the model what to do instead. In the live run
the model hit it at step 13, wrote its verification script to the workspace, ran it,
and deleted it once satisfied — the message redirected rather than blocked, which is
the standard every refusal here is written to.

## Two bugs found by mutation testing

Worth recording because both were in code that looked obviously correct.

**The timeout did not bound the tool.** After killing the child, the code awaited the
output reader to EOF. A pipe stays open as long as anything holds its write end, so
an orphaned grandchild kept the drain running well past the timeout — the mutant that
killed only the direct child took 24 seconds to return from a 2-second timeout. The
drain is now bounded by a grace period and the partial output is kept, since a
command that hangs has usually already printed the interesting part.

**`killpg` could kill Skippy.** With `start_new_session` removed, the child shares
Skippy's process group and `os.killpg` signals the agent, the server and everything
else in the process. The mutation demonstrated this by killing the test runner and
its shell instead of producing a failure. `_terminate_tree` now refuses to signal its
own group and falls back to killing just the child. The guard is cheap; the failure it
prevents is severe and silent.

The same round also exposed a test that proved nothing: the original process-tree
test passed against an implementation that killed only the direct child, because the
grandchild inherited stdout, which held the pipe open, which made the reader wait
until the orphan had finished ticking of its own accord. The grandchild's output now
goes to `DEVNULL`.

## Consequences

The verification loop closes. In the live run the model ran the suite before touching
anything, wrote its change, ran the suite, **got a failure it had caused** (a missing
import in the test file it had just written), read it, fixed the cause and re-ran to
green. Before this slice that broken test file would have shipped with a confident
summary. The resulting `stdev` matched `statistics.stdev` on every case checked.

The system prompt now tells the model to run the tests after changing code, and that
a change it has not executed is a guess.

Costs and open edges:

- Runs are slower, because the model now spends steps on verification. That is the
  trade being made deliberately.
- No install or fetch commands by default, so `npm test` fails on a repo with no
  `node_modules`. The model reports that rather than fixing it. `SKIPPY_EXTRA_COMMANDS`
  lets the user opt in per machine.
- Network access is not restricted. A test suite can reach the internet, and blocking
  that needs the same VM boundary as real containment.
- One command at a time, no interactive processes, no long-running servers.
