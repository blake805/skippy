/**
 * Patch planning in the editor must agree with `skippy_edit.py` on the server.
 *
 * This is the whole risk of having two implementations: if they disagree, the model
 * gets different answers depending on whether Cursor happens to be attached, and the
 * bug appears and disappears with the state of the editor. Every case below is one
 * where a port of this drifted from the Python.
 *
 * The corresponding server-side expectations live in tests/test_edit_tools.py, and
 * tests/test_cursor.py asserts the two agree on a shared table of cases.
 */

const assert = require("node:assert");
const { test } = require("node:test");

const { planEdits, replaceNth, countOccurrences } = require("../out/patches.js");

/** A fake filesystem: a plain object of path -> contents. */
function reader(files) {
  return async (path) => (path in files ? files[path] : null);
}

async function plan(files, edits) {
  return planEdits(edits, reader(files));
}

// --- the four divergences ---

test("a replacement containing $& is written literally", async () => {
  // String.prototype.replace expands $& even for a string pattern, so this would
  // have produced "xbx" style corruption while the server wrote the literal text.
  const result = await plan({ "/a.js": "const x = MARK;\n" }, [
    { path: "/a.js", search: "MARK", replace: "'$&'" }
  ]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/a.js"), "const x = '$&';\n");
});

test("dollar patterns in a replacement are all literal", async () => {
  for (const replacement of ["$&", "$1", "$`", "$'", "$$", "a$&b"]) {
    const result = await plan({ "/a.txt": "MARK" }, [
      { path: "/a.txt", search: "MARK", replace: replacement }
    ]);
    assert.deepStrictEqual(result.failures, [], `failed for ${replacement}`);
    assert.strictEqual(result.staged.get("/a.txt"), replacement, `wrong for ${replacement}`);
  }
});

test("replace_all is literal too", async () => {
  const result = await plan({ "/a.txt": "M and M" }, [
    { path: "/a.txt", search: "M", replace: "$&", replace_all: true }
  ]);
  assert.strictEqual(result.staged.get("/a.txt"), "$& and $&");
});

test("occurrence counts non-overlapping matches, like the server", () => {
  // "aa" in "aaaa" is two non-overlapping matches, at 0 and 2. Advancing by one
  // character instead makes occurrence 2 the span at index 1, which overlaps the
  // first and is not what was asked for.
  assert.strictEqual(countOccurrences("aaaa", "aa"), 2);
  assert.strictEqual(replaceNth("aaaa", "aa", "X", 1), "Xaa");
  assert.strictEqual(replaceNth("aaaa", "aa", "X", 2), "aaX");
});

test("an occurrence past the end is null, not the text unchanged", () => {
  // A silent no-op reported as a successful edit is worse than an error.
  assert.strictEqual(replaceNth("aaaa", "aa", "X", 3), null);
  assert.strictEqual(replaceNth("abc", "z", "X", 1), null);
});

test("replace_all and occurrence together are refused", async () => {
  // The server refuses both rather than picking one, so preferring replace_all here
  // would apply an edit the server would have rejected.
  const result = await plan({ "/a.txt": "x x x" }, [
    { path: "/a.txt", search: "x", replace: "y", replace_all: true, occurrence: 2 }
  ]);
  assert.strictEqual(result.failures.length, 1);
  assert.match(result.failures[0].reason, /not both/);
  assert.strictEqual(result.staged.size, 0);
});

// --- uniqueness, the core contract ---

test("an ambiguous search is refused", async () => {
  const result = await plan({ "/a.py": "x = 1\nx = 1\n" }, [
    { path: "/a.py", search: "x = 1", replace: "x = 2" }
  ]);
  assert.strictEqual(result.failures.length, 1);
  assert.match(result.failures[0].reason, /matched 2 times/);
});

test("a unique search applies", async () => {
  const result = await plan({ "/a.py": "x = 1\ny = 2\n" }, [
    { path: "/a.py", search: "x = 1", replace: "x = 99" }
  ]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/a.py"), "x = 99\ny = 2\n");
});

test("search text that is not there is refused", async () => {
  const result = await plan({ "/a.py": "x = 1\n" }, [
    { path: "/a.py", search: "nowhere", replace: "y" }
  ]);
  assert.match(result.failures[0].reason, /not found/);
});

test("occurrence out of range names how many were found", async () => {
  const result = await plan({ "/a.txt": "x x" }, [
    { path: "/a.txt", search: "x", replace: "y", occurrence: 5 }
  ]);
  assert.match(result.failures[0].reason, /out of range \(found 2\)/);
});

// --- all or nothing ---

test("one bad edit fails the whole set", async () => {
  const result = await plan({ "/a.py": "good\n", "/b.py": "also good\n" }, [
    { path: "/a.py", search: "good", replace: "better" },
    { path: "/b.py", search: "missing", replace: "x" }
  ]);
  assert.strictEqual(result.failures.length, 1);
  // The caller applies nothing when there are failures; the first edit staged fine
  // but must never reach the editor on its own.
  assert.strictEqual(result.failures[0].path, "/b.py");
});

test("later edits see earlier ones in the same set", async () => {
  const result = await plan({ "/a.py": "one\n" }, [
    { path: "/a.py", search: "one", replace: "two" },
    { path: "/a.py", search: "two", replace: "three" }
  ]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/a.py"), "three\n");
});

// --- create and delete ---

test("create writes a new file", async () => {
  const result = await plan({}, [{ path: "/new.py", action: "create", content: "hello\n" }]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/new.py"), "hello\n");
  assert.strictEqual(result.actions.get("/new.py"), "create");
});

test("create over an existing file needs overwrite", async () => {
  const result = await plan({ "/a.py": "existing\n" }, [
    { path: "/a.py", action: "create", content: "new\n" }
  ]);
  assert.match(result.failures[0].reason, /already exists/);
});

test("create with overwrite replaces the contents and is not a create", async () => {
  const result = await plan({ "/a.py": "existing\n" }, [
    { path: "/a.py", action: "create", content: "new\n", overwrite: true }
  ]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/a.py"), "new\n");
  // Not "create": the editor must replace the buffer's range rather than try to
  // create a file that is already there.
  assert.strictEqual(result.actions.get("/a.py"), "overwrite");
});

test("editing a file that does not exist points at create", async () => {
  const result = await plan({}, [{ path: "/gone.py", search: "x", replace: "y" }]);
  assert.match(result.failures[0].reason, /use action 'create'/);
});

test("delete stages a removal", async () => {
  const result = await plan({ "/a.py": "bye\n" }, [{ path: "/a.py", action: "delete" }]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/a.py"), null);
});

test("deleting a file that is not there is refused", async () => {
  const result = await plan({}, [{ path: "/gone.py", action: "delete" }]);
  assert.match(result.failures[0].reason, /does not exist/);
});

test("an unknown action is refused rather than guessed at", async () => {
  const result = await plan({ "/a.py": "x\n" }, [{ path: "/a.py", action: "rename" }]);
  assert.match(result.failures[0].reason, /unknown action/);
});

test("a missing path is refused", async () => {
  const result = await plan({}, [{ search: "x", replace: "y" }]);
  assert.match(result.failures[0].reason, /missing 'path'/);
});

// --- no-ops ---

test("an edit that changes nothing is not reported as a change", async () => {
  const result = await plan({ "/a.py": "same\n" }, [
    { path: "/a.py", search: "same", replace: "same" }
  ]);
  assert.deepStrictEqual(result.failures, []);
  assert.deepStrictEqual(result.order, []);
  assert.strictEqual(result.actions.size, 0);
});

test("a real change among no-ops still applies", async () => {
  const result = await plan({ "/a.py": "same\n", "/b.py": "old\n" }, [
    { path: "/a.py", search: "same", replace: "same" },
    { path: "/b.py", search: "old", replace: "new" }
  ]);
  assert.deepStrictEqual(result.order, ["/b.py"]);
});

// --- text that tends to break naive implementations ---

test("CRLF content is not rewritten", async () => {
  const result = await plan({ "/a.txt": "one\r\ntwo\r\n" }, [
    { path: "/a.txt", search: "one", replace: "ONE" }
  ]);
  assert.strictEqual(result.staged.get("/a.txt"), "ONE\r\ntwo\r\n");
});

test("a multi-line search works", async () => {
  const result = await plan({ "/a.py": "def f():\n    pass\n" }, [
    { path: "/a.py", search: "def f():\n    pass", replace: "def f():\n    return 1" }
  ]);
  assert.strictEqual(result.staged.get("/a.py"), "def f():\n    return 1\n");
});

test("non-ascii content survives", async () => {
  const result = await plan({ "/a.txt": "café ☕\n" }, [
    { path: "/a.txt", search: "café", replace: "thé" }
  ]);
  assert.strictEqual(result.staged.get("/a.txt"), "thé ☕\n");
});

test("regex metacharacters in search are literal", async () => {
  const result = await plan({ "/a.py": "value = x[0].y(1)\n" }, [
    { path: "/a.py", search: "x[0].y(1)", replace: "z" }
  ]);
  assert.deepStrictEqual(result.failures, []);
  assert.strictEqual(result.staged.get("/a.py"), "value = z\n");
});
