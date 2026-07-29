# ADR 0009 — One write tool, all or nothing

- **Status:** accepted
- **Date:** 2026-07-29

## Context

An agent implementing a feature edits several files at once. A rename touches the
definition, the exports, the call sites and the tests. The question is what
happens when the third of five edits is wrong.

If edits are written as they are validated, the answer is that the repo is left
broken in a way that is expensive to diagnose. The agent's next observation is a
pile of import errors unrelated to what it was actually doing, it cannot tell
which edits landed, and neither can the user reading the diff afterwards. The
model will usually try to repair the damage, which is where a bad situation
becomes a worse one.

There is also the question of how an edit is addressed. Line numbers go stale as
soon as an earlier edit in the same batch shifts them. Unified diff hunks are
something models reproduce badly, because the context lines have to be exact and
there is no feedback until the patch fails to apply.

## Decision

**One tool, `apply_patch`, taking a list of edits across any number of files, and
it is all-or-nothing.** Every edit is validated against staged in-memory content
first. If any edit fails, nothing is written and the result lists every problem at
once, so the model can fix a batch in one more turn instead of one turn per
problem.

Staging also makes edits compose: each one validates against the result of the
previous, so several edits to the same file in one call work, and a whole rename
is a single call.

**Edits are byte-for-byte search/replace.** Exact text either matches or it does
not. When it does not, the error says the text must match including indentation
and blank lines, and tells the model to re-read the file — which is a recoverable
instruction, unlike a failed diff application. A search matching more than once is
rejected as ambiguous rather than guessed at, and the error names the two ways to
disambiguate (`replace_all`, `occurrence`).

`dry_run` returns the same diff without writing, so a large change can be checked
before it lands.

Writes go through a temp file in the same directory followed by `os.replace`,
which is atomic within a filesystem: an interrupted write leaves the original
intact rather than a truncated file. File mode is preserved, so patching a shell
script does not quietly make it non-executable.

Being one tool is itself the decision. Every mutation in the system goes through a
single function, which means the sandbox check, the text-safety checks and the
atomicity guarantee each exist in exactly one place.

## Text safety

This is the only module that can destroy data, so it is strict about what it will
touch:

- **Files are decoded as strict UTF-8.** A file that is not valid UTF-8 is refused.
- **Files with a NUL byte in the first 8KB are refused** as binary.
- **Line endings are preserved.** CRLF is detected, normalized to `\n` for
  matching, and restored on write. CRLF only wins if it is the dominant style, so
  one pasted line cannot flip a whole file. Search strings are normalized the same
  way, so a model echoing back text it read from a CRLF file still matches.
- **Files above 8MB are refused**, the same ceiling as `read_file`.

## The journal

Pre-images are written to `patch_journal_root()` before any file is touched, along
with a manifest recording absolute paths and a restore instruction.

Lineage B wrote these too, and they were useless. Rollback worked from memory, no
code or tool ever read the manifest, and the entries recorded a mangled relative
path (`calc__ops.py`) rather than anything that could be restored automatically.
It looked like a safety net without being one, which is worse than not having it,
because it invites trust it cannot honour.

Kept here on the condition that recovery is a real procedure: the manifest holds
absolute paths, states what to do for both edited and created files, and has been
verified by restoring a four-file refactor from the journal alone and confirming
the repo came back byte-identical.

The journal is best-effort. If it cannot be written the patch still proceeds,
because in-memory rollback is the actual atomicity guarantee and it is unaffected.
The journal covers only what rollback cannot: a crash or a kill signal partway
through the write loop.

## Consequences

- A rejected patch costs one turn and changes nothing. This is the common case and
  it is cheap.
- **A failed rollback is reported as a mixed state, loudly, naming the journal
  directory.** Lineage B logged the failure and still returned "rolled back N
  files", which is a lie in exactly the situation where the user most needs the
  truth. It is the one outcome that requires human intervention, so it says so.
- The diff is generated from staged content rather than by re-reading the disk,
  and uses workspace-relative paths, so it is reviewable and contains no absolute
  paths to leak.
- Deleting a file is supported but renaming is not; a rename is a create plus a
  delete in one call, which is atomic anyway and keeps the action set to three.
- Nothing here is interactive. Approval routing for writes, and applying patches
  through Cursor's undo stack instead of directly to disk, are separate concerns
  that layer on top of this tool rather than changing it.

## Departures from the lineage B implementation

Three of these were data-destroying, and each has a test named after the
corruption it caused.

**Non-UTF-8 files were silently corrupted.** It read with `errors="replace"` and
wrote the result back, so a latin-1 copyright sign in an old C header became
U+FFFD and the original byte was gone — on any edit to that file, including one
that touched an unrelated line. Verified against the real code before rewriting
it. RE work on vendor source is exactly where this would have hit.

**CRLF files were entirely rewritten.** Reading in text mode converts CRLF to LF
in memory and writing it back emits LF, so changing one word in a CRLF file
produced a whole-file diff. The real change is then invisible in review.

**`occurrence` targeted the wrong text.** Validation used `str.count`, which counts
non-overlapping matches, while `_replace_nth` advanced one character at a time and
walked overlapping ones. For search `aa` in `aaaa`, `occurrence: 2` was accepted
and then edited the span at index 1 — text the model had not pointed at.

Two smaller ones: `replace_all` together with `occurrence` silently ignored
`occurrence`, and is now rejected; and a diff line lacking a trailing newline
spliced into the next hunk header, making the diff unreadable at exactly the
moment it mattered.
