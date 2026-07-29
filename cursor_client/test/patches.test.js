/**
 * The extension's patch semantics must match `skippy_agent_tools.apply_patch`
 * exactly, otherwise the same edit set behaves differently depending on whether
 * Cursor happens to be attached. These cases mirror tests/test_apply_patch.py.
 *
 * Run with: npm run compile && npm test
 */

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { planEdits, countOccurrences, replaceNth } = require("../out/patches.js");

const FILES = {
  "/repo/ops.py": "def add(a, b):\n    return a + b\n",
  "/repo/dup.py": "x = 1\nx = 1\nx = 1\n"
};

function reader(overrides = {}) {
  const files = { ...FILES, ...overrides };
  return async (path) => (path in files ? files[path] : null);
}

test("counts and targets occurrences", () => {
  assert.equal(countOccurrences("aaa", "a"), 3);
  assert.equal(countOccurrences("abab", "ab"), 2);
  assert.equal(countOccurrences("abc", "z"), 0);
  assert.equal(replaceNth("a\na\na\n", "a", "B", 2), "a\nB\na\n");
});

test("applies a multi-file edit set", async () => {
  const plan = await planEdits(
    [
      { path: "/repo/ops.py", action: "edit", search: "return a + b", replace: "return a + b  # sum" },
      { path: "/repo/new.md", action: "create", content: "# new\n" }
    ],
    reader()
  );

  assert.deepEqual(plan.failures, []);
  assert.deepEqual(plan.order, ["/repo/ops.py", "/repo/new.md"]);
  assert.match(plan.staged.get("/repo/ops.py"), /# sum/);
  assert.equal(plan.staged.get("/repo/new.md"), "# new\n");
  assert.equal(plan.actions.get("/repo/new.md"), "create");
});

test("a missing search string fails the whole set", async () => {
  const plan = await planEdits(
    [
      { path: "/repo/ops.py", action: "edit", search: "return a + b", replace: "return 0" },
      { path: "/repo/ops.py", action: "edit", search: "not here", replace: "x" }
    ],
    reader()
  );

  assert.equal(plan.failures.length, 1);
  assert.equal(plan.failures[0].index, 1);
  assert.match(plan.failures[0].reason, /not found/);
});

test("sequential edits to one file stack", async () => {
  const plan = await planEdits(
    [
      { path: "/repo/ops.py", action: "edit", search: "return a + b", replace: "return a + b  # step1" },
      { path: "/repo/ops.py", action: "edit", search: "# step1", replace: "# step2" }
    ],
    reader()
  );

  assert.deepEqual(plan.failures, []);
  assert.match(plan.staged.get("/repo/ops.py"), /# step2/);
  assert.doesNotMatch(plan.staged.get("/repo/ops.py"), /# step1/);
});

test("an ambiguous search string is rejected", async () => {
  const plan = await planEdits(
    [{ path: "/repo/dup.py", action: "edit", search: "x = 1", replace: "x = 2" }],
    reader()
  );

  assert.equal(plan.failures.length, 1);
  assert.match(plan.failures[0].reason, /matched 3 times/);
});

test("replace_all and occurrence target explicitly", async () => {
  const all = await planEdits(
    [{ path: "/repo/dup.py", action: "edit", search: "x = 1", replace: "x = 9", replace_all: true }],
    reader()
  );
  assert.equal(all.staged.get("/repo/dup.py"), "x = 9\nx = 9\nx = 9\n");

  const second = await planEdits(
    [{ path: "/repo/dup.py", action: "edit", search: "x = 1", replace: "x = 9", occurrence: 2 }],
    reader()
  );
  assert.equal(second.staged.get("/repo/dup.py"), "x = 1\nx = 9\nx = 1\n");

  const outOfRange = await planEdits(
    [{ path: "/repo/dup.py", action: "edit", search: "x = 1", replace: "x = 9", occurrence: 9 }],
    reader()
  );
  assert.match(outOfRange.failures[0].reason, /out of range/);
});

test("create refuses to clobber unless told to", async () => {
  const blocked = await planEdits(
    [{ path: "/repo/ops.py", action: "create", content: "wiped" }],
    reader()
  );
  assert.match(blocked.failures[0].reason, /already exists/);

  const forced = await planEdits(
    [{ path: "/repo/ops.py", action: "create", content: "wiped", overwrite: true }],
    reader()
  );
  assert.deepEqual(forced.failures, []);
  assert.equal(forced.staged.get("/repo/ops.py"), "wiped");
  assert.equal(forced.actions.get("/repo/ops.py"), "overwrite");
});

test("delete requires an existing file", async () => {
  const missing = await planEdits([{ path: "/repo/ghost.py", action: "delete" }], reader());
  assert.match(missing.failures[0].reason, /does not exist/);

  const removed = await planEdits([{ path: "/repo/ops.py", action: "delete" }], reader());
  assert.deepEqual(removed.failures, []);
  assert.equal(removed.staged.get("/repo/ops.py"), null);
});

test("editing a nonexistent file points at create", async () => {
  const plan = await planEdits(
    [{ path: "/repo/ghost.py", action: "edit", search: "a", replace: "b" }],
    reader()
  );
  assert.match(plan.failures[0].reason, /use action 'create'/);
});

test("unknown actions and missing paths are reported", async () => {
  const plan = await planEdits(
    [{ path: "/repo/ops.py", action: "teleport" }, { action: "edit" }],
    reader()
  );
  assert.equal(plan.failures.length, 2);
  assert.match(plan.failures[0].reason, /unknown action/);
  assert.match(plan.failures[1].reason, /missing 'path'/);
});

test("a no-op edit drops out of the plan", async () => {
  const plan = await planEdits(
    [{ path: "/repo/ops.py", action: "edit", search: "return a + b", replace: "return a + b" }],
    reader()
  );
  assert.deepEqual(plan.failures, []);
  assert.deepEqual(plan.order, []);
});
