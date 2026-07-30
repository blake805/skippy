/**
 * The editor's patch planning must agree with the server's.
 *
 * Both sides read tests/fixtures/patch_parity.json. This is the only real defence
 * against the two implementations drifting: if they disagree, the model gets different
 * answers depending on whether Cursor happens to be attached, and the bug comes and
 * goes with the state of the editor rather than with the code.
 *
 * tests/test_cursor.py runs the same table through skippy_edit.apply_patch.
 */

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const { planEdits } = require("../out/patches.js");

const FIXTURE = path.join(__dirname, "..", "..", "tests", "fixtures", "patch_parity.json");
const { cases } = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));

test("the parity table is actually loaded", () => {
  // A silently empty table would make every test below vacuously pass, which is the
  // one way a parity check can fail without anyone noticing.
  assert.ok(cases.length >= 20, `expected a populated table, got ${cases.length}`);
});

for (const testCase of cases) {
  test(`parity: ${testCase.name}`, async () => {
    // Paths in the fixture are relative; the planner works in absolute paths, so the
    // tree is given a fake root that matches what the server's sandbox would produce.
    const root = "/workspace";
    const files = {};
    for (const [relative, content] of Object.entries(testCase.files)) {
      files[path.join(root, relative)] = content;
    }
    const edits = testCase.edits.map((edit) =>
      edit.path === undefined ? edit : { ...edit, path: path.join(root, edit.path) }
    );

    const result = await planEdits(edits, async (p) => (p in files ? files[p] : null));
    const ok = result.failures.length === 0;

    assert.strictEqual(
      ok,
      testCase.expect.ok,
      `expected ok=${testCase.expect.ok}, got ${ok}` +
        (result.failures.length ? ` (${result.failures.map((f) => f.reason).join("; ")})` : "")
    );

    if (!testCase.expect.files) {
      return;
    }
    for (const [relative, expected] of Object.entries(testCase.expect.files)) {
      const absolute = path.join(root, relative);
      if (!ok) {
        // A rejected set changes nothing, so the tree must be exactly as it started.
        assert.strictEqual(files[absolute] ?? null, expected, `${relative} should be untouched`);
        continue;
      }
      const staged = result.staged.has(absolute) ? result.staged.get(absolute) : files[absolute] ?? null;
      assert.strictEqual(staged, expected, `${relative} content`);
    }
  });
}
