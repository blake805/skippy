/**
 * Search/replace patch planning, mirroring `skippy_edit.apply_patch`.
 *
 * The semantics have to match the server exactly. If they do not, the model gets
 * different answers depending on whether Cursor happens to be attached, which is the
 * worst kind of bug to own: it appears and disappears with the state of the editor.
 *
 * Four places where a port of this drifted from the Python and had to be corrected are
 * marked below. They are all cases where the editor path would have accepted an edit
 * the server rejects, or written something different from what the server would write.
 */

export type EditAction = "edit" | "create" | "delete";

export interface Edit {
  path: string;
  action?: EditAction;
  search?: string;
  replace?: string;
  content?: string;
  occurrence?: number;
  replace_all?: boolean;
  overwrite?: boolean;
}

export interface EditFailure {
  index: number;
  path: string;
  reason: string;
}

/** null content means "delete this file". */
export type StagedContent = string | null;

export interface PlanResult {
  staged: Map<string, StagedContent>;
  order: string[];
  actions: Map<string, string>;
  failures: EditFailure[];
}

/**
 * Replace literally, never as a regex replacement pattern.
 *
 * `String.prototype.replace` expands `$&`, `$1`, `` $` `` and `$'` in the *replacement*
 * even when the pattern is a plain string, so a replacement containing a dollar sign
 * silently becomes something else. Python's `str.replace` is literal, so the editor
 * path would have written different bytes than the server for any patch touching shell
 * variables, regex code, or a `$1` in a snippet. Splitting on the needle and joining
 * cannot interpret anything.
 */
function replaceLiteral(text: string, search: string, replacement: string, all: boolean): string {
  if (all) {
    return text.split(search).join(replacement);
  }
  const at = text.indexOf(search);
  if (at === -1) {
    return text;
  }
  return text.slice(0, at) + replacement + text.slice(at + search.length);
}

/**
 * Replace the Nth *non-overlapping* occurrence, 1-based.
 *
 * The step of `search.length` is what makes this agree with `countOccurrences`, which
 * is also non-overlapping. Advancing by one character instead means that for search
 * "aa" in "aaaa" the count says two occurrences, and occurrence 2 resolves to the span
 * at index 1 — a match overlapping the first, and not the one the model asked for. The
 * server had exactly this bug and fixed it; this is the same fix.
 *
 * Returns null when the occurrence is not there, rather than the text unchanged. A
 * silent no-op reported as a successful edit is the one outcome worse than an error.
 */
export function replaceNth(
  text: string,
  search: string,
  replacement: string,
  which: number
): string | null {
  let position = -1;
  for (let seen = 0; seen < which; seen += 1) {
    position = text.indexOf(search, position < 0 ? 0 : position + search.length);
    if (position === -1) {
      return null;
    }
  }
  return text.slice(0, position) + replacement + text.slice(position + search.length);
}

export function countOccurrences(haystack: string, needle: string): number {
  if (!needle) {
    return 0;
  }
  let count = 0;
  let index = haystack.indexOf(needle);
  while (index !== -1) {
    count += 1;
    index = haystack.indexOf(needle, index + needle.length);
  }
  return count;
}

/**
 * Validate an edit set against staged content. `readFile` returns null for a file
 * that does not exist. Nothing is written here: a caller applies the plan only if
 * `failures` is empty, which is what makes an edit set all-or-nothing.
 */
export async function planEdits(
  edits: Edit[],
  readFile: (absolutePath: string) => Promise<string | null>
): Promise<PlanResult> {
  const staged = new Map<string, StagedContent>();
  const original = new Map<string, StagedContent>();
  const actions = new Map<string, string>();
  const order: string[] = [];
  const failures: EditFailure[] = [];

  for (let index = 0; index < edits.length; index += 1) {
    const edit = edits[index];
    const path = edit?.path;
    if (!path) {
      failures.push({ index, path: "", reason: "missing 'path'" });
      continue;
    }
    const action: EditAction = (edit.action as EditAction) || "edit";

    if (!staged.has(path)) {
      const existing = await readFile(path);
      staged.set(path, existing);
      original.set(path, existing);
      order.push(path);
    }
    const current = staged.get(path) ?? null;

    if (action === "create") {
      if (typeof edit.content !== "string") {
        failures.push({ index, path, reason: "'create' requires 'content'" });
        continue;
      }
      if (current !== null && !edit.overwrite) {
        failures.push({ index, path, reason: "file already exists; use 'edit' or pass overwrite" });
        continue;
      }
      staged.set(path, edit.content);
      actions.set(path, original.get(path) === null ? "create" : "overwrite");
      continue;
    }

    if (action === "delete") {
      if (current === null) {
        failures.push({ index, path, reason: "cannot delete, file does not exist" });
        continue;
      }
      staged.set(path, null);
      actions.set(path, "delete");
      continue;
    }

    if (action !== "edit") {
      failures.push({ index, path, reason: `unknown action '${action}'` });
      continue;
    }

    if (current === null) {
      failures.push({ index, path, reason: "file does not exist; use action 'create'" });
      continue;
    }
    if (!edit.search) {
      failures.push({ index, path, reason: "'edit' requires non-empty 'search'" });
      continue;
    }
    if (typeof edit.replace !== "string") {
      failures.push({ index, path, reason: "'edit' requires 'replace'" });
      continue;
    }
    // The server refuses both together rather than picking one. Silently preferring
    // replace_all here would apply an edit the server would have rejected.
    if (edit.replace_all && edit.occurrence !== undefined && edit.occurrence !== null) {
      failures.push({ index, path, reason: "pass either 'replace_all' or 'occurrence', not both" });
      continue;
    }

    const hits = countOccurrences(current, edit.search);
    if (hits === 0) {
      failures.push({ index, path, reason: "'search' text not found" });
      continue;
    }

    if (edit.replace_all) {
      staged.set(path, replaceLiteral(current, edit.search, edit.replace, true));
    } else if (edit.occurrence !== undefined && edit.occurrence !== null) {
      const which = Number(edit.occurrence);
      if (!Number.isInteger(which) || which < 1 || which > hits) {
        failures.push({
          index,
          path,
          reason: `'occurrence' ${edit.occurrence} out of range (found ${hits})`
        });
        continue;
      }
      const replaced = replaceNth(current, edit.search, edit.replace, which);
      if (replaced === null) {
        failures.push({ index, path, reason: `occurrence ${which} of the search text disappeared while staging` });
        continue;
      }
      staged.set(path, replaced);
    } else if (hits > 1) {
      failures.push({
        index,
        path,
        reason: `'search' matched ${hits} times; add context or pass replace_all`
      });
      continue;
    } else {
      staged.set(path, replaceLiteral(current, edit.search, edit.replace, false));
    }
    if (!actions.has(path)) {
      actions.set(path, "edit");
    }
  }

  // Drop no-ops so an unchanged file never shows up as an edit in the editor.
  for (const path of [...order]) {
    if (original.get(path) === staged.get(path)) {
      staged.delete(path);
      actions.delete(path);
      order.splice(order.indexOf(path), 1);
    }
  }

  return { staged, order, actions, failures };
}
