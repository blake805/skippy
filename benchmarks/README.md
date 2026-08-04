# benchmarks

Measurements that back a claim in an ADR. Each one exists because the alternative was
arguing about the answer.

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
