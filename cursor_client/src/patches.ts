/**
 * Search/replace patch application, mirroring `skippy_agent_tools.apply_patch`.
 *
 * The semantics have to match the server exactly or the model gets different
 * answers depending on whether Cursor happens to be attached: a `search` string
 * must be unique unless `replace_all` or `occurrence` says otherwise, and a whole
 * edit set either applies or it does not.
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

export function replaceNth(text: string, search: string, replacement: string, which: number): string {
  let position = -1;
  for (let seen = 0; seen < which; seen += 1) {
    position = text.indexOf(search, position + 1);
    if (position === -1) {
      return text;
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
 * that does not exist.
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

    const hits = countOccurrences(current, edit.search);
    if (hits === 0) {
      failures.push({ index, path, reason: "'search' text not found" });
      continue;
    }

    if (edit.replace_all) {
      staged.set(path, current.split(edit.search).join(edit.replace));
    } else if (edit.occurrence !== undefined && edit.occurrence !== null) {
      const which = Number(edit.occurrence);
      if (!Number.isInteger(which) || which < 1 || which > hits) {
        failures.push({ index, path, reason: `'occurrence' ${edit.occurrence} out of range (found ${hits})` });
        continue;
      }
      staged.set(path, replaceNth(current, edit.search, edit.replace, which));
    } else if (hits > 1) {
      failures.push({ index, path, reason: `'search' matched ${hits} times; add context or pass replace_all` });
      continue;
    } else {
      staged.set(path, current.replace(edit.search, edit.replace));
    }
    if (!actions.has(path)) {
      actions.set(path, "edit");
    }
  }

  // Drop no-ops so an unchanged file never shows up as an edit in the editor.
  for (const path of [...order]) {
    if (original.get(path) === staged.get(path)) {
      staged.delete(path);
      order.splice(order.indexOf(path), 1);
    }
  }

  return { staged, order, actions, failures };
}
