# benchmarks

Measurements that back a claim in an ADR. Each one exists because the alternative was
arguing about the answer.

## `python -m tests.agent_eval` — does the coding agent finish the job?

The scoreboard. Ten tasks against throwaway copies of the small repos in
`tests/fixtures/eval_repos`, each graded by machine: tests green, a symbol present, a
file untouched, a summary matching a pattern. No model judges the output — a
model-as-judge grades fluency, and fluency is the one thing here that needs no help.

It exists because `prompts.py` is several hundred lines of pure behaviour with no
regression test, so every edit to it was previously an argument nobody could settle. Each
task names the line of `AGENT_SYSTEM` it defends, so a red task says what to go and
change rather than only that the number went down.

```bash
python -m tests.agent_eval --list
python -m tests.agent_eval --save                    # the run, and the diff against last
python -m tests.agent_eval --task rename_across_files --verbose
```

Needs a model server, and it is slow — ten tasks at up to 25 steps on the heavy role is a
coffee. Results land in `benchmarks/agent/`, and each run prints which tasks changed
verdict since the previous one, which is the only number that actually tells you whether
the last change helped.

A task the endpoint killed is reported as ERROR, retried once (`--retry-errors N`), and
excluded from the pass rate. The first live baseline is why: the heavy server dropped
three connections, and because the loop turns `ModelError` into a `failed` outcome
instead of raising, all three were graded like ordinary runs. Two scored as the agent
getting the task wrong — one of them a half-applied multi-file edit, which is exactly the
failure that task exists to catch and so looked entirely convincing. The third had
finished its edits before the server went, satisfied every grader, and was recorded
green. Loud contamination is survivable; a crashed run scoring as a pass is not.

Four of the tasks pass only by *not* doing something: not editing a test to make it pass,
not inventing work when the thing asked about does not exist, not touching files the task
never mentioned. A set of features alone would score a busy fabricator top marks.

`tests/test_agent_eval.py` is what CI can check without a model, and the load-bearing
part of it asserts that **every grader fails on the untouched repo**. That is not
theoretical: it caught a task on its first run that passed with the agent doing nothing,
because the README already contained the word the grader looked for.

## `vision_spike.py` — is a local vision model good enough to wire in?

Asked before building, because wiring vision in properly is a phase of work — a role,
multimodal content through `skippy_llm` and the transcript, a wire path, a picker in two
Swift apps — and all of it is wasted if the answers are not good enough. The thing you
actually want here is a part number read off a chip in a phone photo under shop
lighting, which is the hardest case for a local VL model and the one worth testing first.

A VL model needs `mlx-vlm`; `mlx_lm.server` does not load these weights. It speaks the
same OpenAI-compatible `/v1/chat/completions`, which is why nothing else has to change.

```bash
pip install mlx-vlm
python -m mlx_vlm.server --model mlx-community/Qwen3-VL-32B-Instruct-8bit --port 8084

SKIPPY_VISION_URL=http://127.0.0.1:8084/v1/chat/completions \
SKIPPY_VISION_MODEL=mlx-community/Qwen3-VL-32B-Instruct-8bit \
python benchmarks/vision_spike.py bench-photos/*.jpg --repeats 2 \
    --ask "Read every part number you can make out. Say which you are unsure of."
```

Worth running twice, against two different kinds of model. `mlx-vlm` now carries a
class of OCR specialists that did not exist when this was written — DeepSeek-OCR,
DOTS-OCR, GLM-OCR, PaddleOCR-VL — and reading a part number off a chip is their whole
job, where it is a side skill for a general VL model. But "what is this board" and
"which connector is that" are not OCR questions, so if the specialist wins on text and
loses on everything else, the answer is two models or none, and that is worth knowing
before a role gets added to the registry.

It tries the OpenAI request shape (base64 data URL) and falls back to mlx-vlm's own
(`input_image` with a path), reporting which was accepted — the real integration will
need that answer too.

It changes nothing and registers nothing — `query_message` already passes message
content through untouched, so a multimodal request needs no support added to find out
whether the model is useful. A registry entry nothing reads is the mistake that left an
`AGENT_CODER_ROLE` in `skippy_llm` for months; a spike should be able to conclude "not
yet" and leave no trace.

Three questions decide it: does it read small text, does it invent detail when it
cannot (ask twice — instability is the tell), and is it fast enough to sit in a
conversation rather than needing the background-and-follow-up treatment research runs
get.

## `decode_speed.py` — is a draft model worth it?

More of the gap between this and a hosted frontier model is latency than is reasoning.
The heavy role decodes at roughly 13.5 tok/s, so a two-hundred-token step is fifteen
seconds of watching a cursor and an agent run is dozens of steps.

Speculative decoding is the specific bet: the 30B drafts, the 480B verifies in one pass,
and every accepted token is one the big model did not generate serially. It is exact, so
it costs nothing in quality — but the acceptance rate on a 4-bit MoE with a
different-architecture draft could be anything, which is why this measures rather than
asserts. Both models fit at once (roughly 240GB and 17GB of 512GB), so the draft is
nearly free in space.

```bash
python benchmarks/decode_speed.py --role heavy --label baseline --save
# restart the server with --draft-model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit
python benchmarks/decode_speed.py --role heavy --label draft-30b --save
python benchmarks/decode_speed.py --compare
```

Three prompt shapes, because one average hides the trade: `chat` is almost pure decode
(where a draft helps most), `agent_step` is a 12K-token prefill and a short answer (which
is what every step of a real run looks like, and mostly prefill), and `patch` generates
code. A large gain on `chat` with nothing on `agent_step` is the expected result, not a
broken run. Token counts are characters over four and say so — the honest number would
need `skippy_llm` to surface usage, which is not worth a second code path for a
benchmark.

## `re_model_compare.py` — does the RE lane need the 480B?

Backs the model-sizing table in
[ADR 0018](../docs/adr/0018-rizin-structured-tools.md). ADR 0007 chose the 480B for the
planner role on tool discipline, and ADR 0018 changed the premise that choice was made
under by giving the model one function at a time instead of tool-sized regions. This runs
the same reverse-engineering task on each model against a target whose answers are known
in advance, so a run can be scored rather than admired.

`updater.c` is the target: a firmware update path that validates a magic number and a
CRC32 and has no signature anywhere, with a provisioning key left unused in the binary.
The weakness is deliberate and is the thing a run is scored on finding — and on grading
correctly, since ADR 0017 made severity the order in which work reaches a coding session.

```bash
cc -O1 -g benchmarks/updater.c -o benchmarks/updater

# The workspace must not contain the source. A model that can read updater.c will, and
# then the run measures reading C rather than reverse engineering — which is exactly the
# mistake the first pass made.
mkdir -p /tmp/re_target && cp benchmarks/updater /tmp/re_target/

python benchmarks/re_model_compare.py \
    --target /tmp/re_target/updater \
    --workspace /tmp/re_target \
    --notes /tmp/re_compare \
    --steps 30 --roles fast,heavy
```

Both models need to be served — `fast` is the 30B and `heavy` the 480B, as
`skippy_llm` resolves them. Give both the same step budget: a run that ends in
`max_steps` has been cut off mid-investigation and its finding count says more about the
budget than about the model.

Expect variance. Two 30B runs in this comparison differed by two orders of magnitude in
tool calls, the difference being one extra file in the workspace. Three runs each is
enough for a direction and not enough for a number.
