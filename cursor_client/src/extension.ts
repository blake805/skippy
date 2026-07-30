/**
 * Skippy's hands inside Cursor.
 *
 * The extension is a websocket *client* of the Skippy hub, registering as
 * `client_id=cursor`. The hub sends `{action, task_id, ...}` requests; every reply must
 * echo `task_id` verbatim, because that is what routes it back to the agent coroutine
 * waiting on it.
 *
 * It exists for two things the filesystem cannot give the agent: the editor's live
 * diagnostics, and edits that land in the editor's own undo stack rather than appearing
 * as mysterious changes on disk.
 *
 * There is deliberately no command-execution action. An earlier design had `run_task`
 * shelling out with Node's `exec`, which would have been a second execution path with
 * none of the policy the server's `run_command` enforces — no allowlist, a real shell,
 * and an unauthenticated socket in front of it. The server already knows how to run
 * things safely; the editor does not need to.
 */

import * as vscode from "vscode";
import WebSocket from "ws";

import { Edit, EditFailure, planEdits } from "./patches";

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

/**
 * How long to wait for language servers to re-analyse after an edit.
 *
 * Diagnostics are produced asynchronously, so reading them the instant a patch is
 * applied returns the state from *before* the edit: a new error looks like a clean
 * bill of health, and an error the patch just fixed still looks broken. Either one
 * teaches the agent something false about its own change. So a diagnostics request
 * that names files waits for a change event covering them, and gives up after a bound
 * rather than hanging when a file has no language server at all.
 */
const DIAGNOSTIC_SETTLE_MS = 2_500;
const DIAGNOSTIC_QUIET_MS = 350;

let client: HubClient | undefined;
let statusItem: vscode.StatusBarItem;

interface Request {
  action: string;
  task_id?: string;
  [key: string]: unknown;
}

function config() {
  const settings = vscode.workspace.getConfiguration("skippy");
  return {
    serverUrl: settings.get<string>("serverUrl", "ws://127.0.0.1:8000/ws/factory"),
    clientId: settings.get<string>("clientId", "cursor"),
    autoConnect: settings.get<boolean>("autoConnect", true),
    confirmPatches: settings.get<boolean>("confirmPatches", false)
  };
}

class HubClient {
  private socket: WebSocket | undefined;
  private reconnectTimer: NodeJS.Timeout | undefined;
  private attempts = 0;
  private closing = false;

  constructor(private readonly output: vscode.OutputChannel) {}

  get connected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect(): void {
    const { serverUrl, clientId } = config();
    const url = `${serverUrl}${serverUrl.includes("?") ? "&" : "?"}client_id=${encodeURIComponent(clientId)}`;
    this.closing = false;
    this.output.appendLine(`Connecting to ${url}`);
    setStatus("connecting");

    const socket = new WebSocket(url);
    this.socket = socket;

    socket.on("open", () => {
      this.attempts = 0;
      this.output.appendLine("Connected.");
      setStatus("connected");
      // The hub recognises this and does not treat it as a task request.
      socket.send(JSON.stringify({ type: "hello", client_id: clientId }));
    });

    socket.on("message", (raw: WebSocket.RawData) => {
      void this.handle(raw.toString());
    });

    socket.on("close", () => {
      setStatus("disconnected");
      if (!this.closing) {
        this.scheduleReconnect();
      }
    });

    socket.on("error", (error: Error) => {
      this.output.appendLine(`Socket error: ${error.message}`);
    });
  }

  disconnect(): void {
    this.closing = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    this.socket?.close();
    this.socket = undefined;
    setStatus("disconnected");
  }

  private scheduleReconnect(): void {
    this.attempts += 1;
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** (this.attempts - 1), RECONNECT_MAX_MS);
    this.output.appendLine(`Reconnecting in ${Math.round(delay / 1000)}s`);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private send(payload: unknown): void {
    if (this.connected) {
      this.socket?.send(JSON.stringify(payload));
    }
  }

  private async handle(raw: string): Promise<void> {
    let request: Request;
    try {
      request = JSON.parse(raw) as Request;
    } catch {
      return;
    }
    if (!request.action) {
      // Progress and chat events from the agent; nothing to answer.
      return;
    }
    // Without a task_id there is no waiting coroutine to route a reply to, so
    // answering would put an unroutable message on the wire.
    const taskId = request.task_id;
    if (!taskId) {
      this.output.appendLine(`Ignoring '${request.action}' with no task_id.`);
      return;
    }

    try {
      const result = await dispatch(request);
      this.send({ task_id: taskId, ok: true, result });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.output.appendLine(`Action '${request.action}' failed: ${message}`);
      this.send({ task_id: taskId, ok: false, error: message });
    }
  }
}

async function dispatch(request: Request): Promise<unknown> {
  switch (request.action) {
    case "ping":
      return { pong: true };
    case "get_workspace_roots":
      return getWorkspaceRoots();
    case "get_open_files":
      return getOpenFiles();
    case "get_diagnostics":
      return getDiagnostics((request.paths as string[]) ?? [], Boolean(request.settle));
    case "apply_patches":
      return applyPatches((request.edits as Edit[]) ?? []);
    default:
      throw new Error(`Unsupported action '${request.action}'`);
  }
}

function getWorkspaceRoots(): { roots: { name: string; path: string }[] } {
  const folders = vscode.workspace.workspaceFolders ?? [];
  return { roots: folders.map((folder) => ({ name: folder.name, path: folder.uri.fsPath })) };
}

function getOpenFiles(): { files: { path: string; active: boolean; dirty: boolean; language: string }[] } {
  const activePath = vscode.window.activeTextEditor?.document.uri.fsPath;
  const files = vscode.workspace.textDocuments
    .filter((document) => document.uri.scheme === "file")
    .map((document) => ({
      path: document.uri.fsPath,
      active: document.uri.fsPath === activePath,
      dirty: document.isDirty,
      language: document.languageId
    }));
  return { files };
}

const SEVERITY_NAMES = ["error", "warning", "info", "hint"];

interface Diagnostic {
  path: string;
  line: number;
  col: number;
  severity: string;
  message: string;
  source?: string;
}

function collectDiagnostics(paths: string[]): Diagnostic[] {
  const wanted = new Set(paths);
  const collected: Diagnostic[] = [];

  for (const [uri, entries] of vscode.languages.getDiagnostics()) {
    if (uri.scheme !== "file") {
      continue;
    }
    if (wanted.size > 0 && !wanted.has(uri.fsPath)) {
      continue;
    }
    for (const entry of entries) {
      collected.push({
        path: uri.fsPath,
        line: entry.range.start.line + 1,
        col: entry.range.start.character + 1,
        severity: SEVERITY_NAMES[entry.severity] ?? "info",
        message: entry.message,
        source: entry.source
      });
    }
  }

  // Errors first: they are what the agent needs to act on.
  collected.sort(
    (left, right) => SEVERITY_NAMES.indexOf(left.severity) - SEVERITY_NAMES.indexOf(right.severity)
  );
  return collected;
}

/**
 * Wait until the language servers stop changing their minds about these files.
 *
 * Resolves once no diagnostics event has touched them for `DIAGNOSTIC_QUIET_MS`, or
 * when the overall bound expires. The quiet period matters because analysis often
 * arrives in bursts — a syntax pass, then a type pass — and returning after the first
 * would report a half-formed picture.
 */
async function waitForDiagnostics(paths: string[]): Promise<void> {
  if (paths.length === 0) {
    return;
  }
  const wanted = new Set(paths);
  await new Promise<void>((resolve) => {
    let quiet: NodeJS.Timeout | undefined;
    let finished = false;

    const done = () => {
      if (finished) {
        return;
      }
      finished = true;
      if (quiet) {
        clearTimeout(quiet);
      }
      clearTimeout(ceiling);
      subscription.dispose();
      resolve();
    };

    const ceiling = setTimeout(done, DIAGNOSTIC_SETTLE_MS);
    const subscription = vscode.languages.onDidChangeDiagnostics((event) => {
      if (!event.uris.some((uri) => wanted.has(uri.fsPath))) {
        return;
      }
      if (quiet) {
        clearTimeout(quiet);
      }
      quiet = setTimeout(done, DIAGNOSTIC_QUIET_MS);
    });
  });
}

async function getDiagnostics(paths: string[], settle: boolean): Promise<{ diagnostics: Diagnostic[] }> {
  if (settle) {
    await waitForDiagnostics(paths);
  }
  return { diagnostics: collectDiagnostics(paths) };
}

async function readFileIfPresent(absolutePath: string): Promise<string | null> {
  const uri = vscode.Uri.file(absolutePath);
  try {
    // An open dirty buffer is the truth the user sees, so prefer it over disk.
    const open = vscode.workspace.textDocuments.find(
      (document) => document.uri.fsPath === absolutePath
    );
    if (open) {
      return open.getText();
    }
    const bytes = await vscode.workspace.fs.readFile(uri);
    return Buffer.from(bytes).toString("utf8");
  } catch {
    return null;
  }
}

async function applyPatches(
  edits: Edit[]
): Promise<{ applied: string[]; failed: EditFailure[]; diagnostics: Diagnostic[] }> {
  if (edits.length === 0) {
    return { applied: [], failed: [], diagnostics: [] };
  }

  const plan = await planEdits(edits, readFileIfPresent);
  // All or nothing: a partially applied edit set is worse than a rejected one,
  // because the tree is then in a state neither the model nor the user expects.
  if (plan.failures.length > 0) {
    return { applied: [], failed: plan.failures, diagnostics: [] };
  }
  if (plan.order.length === 0) {
    return { applied: [], failed: [], diagnostics: [] };
  }

  if (config().confirmPatches) {
    const choice = await vscode.window.showInformationMessage(
      `Skippy wants to change ${plan.order.length} file(s).`,
      { modal: true },
      "Apply",
      "Cancel"
    );
    if (choice !== "Apply") {
      return {
        applied: [],
        failed: plan.order.map((path, index) => ({ index, path, reason: "user declined the edit" })),
        diagnostics: []
      };
    }
  }

  const workspaceEdit = new vscode.WorkspaceEdit();
  const toSave: vscode.Uri[] = [];

  for (const path of plan.order) {
    const uri = vscode.Uri.file(path);
    const content = plan.staged.get(path) ?? null;

    if (content === null) {
      workspaceEdit.deleteFile(uri, { ignoreIfNotExists: true });
      continue;
    }

    if (plan.actions.get(path) === "create") {
      workspaceEdit.createFile(uri, { overwrite: false, contents: Buffer.from(content, "utf8") });
    } else {
      const document = await vscode.workspace.openTextDocument(uri);
      const whole = new vscode.Range(
        document.positionAt(0),
        document.positionAt(document.getText().length)
      );
      workspaceEdit.replace(uri, whole, content);
    }
    toSave.push(uri);
  }

  // One WorkspaceEdit, so the whole change set is a single undo step for the user.
  const ok = await vscode.workspace.applyEdit(workspaceEdit);
  if (!ok) {
    return {
      applied: [],
      failed: plan.order.map((path, index) => ({
        index,
        path,
        reason: "editor rejected the workspace edit"
      })),
      diagnostics: []
    };
  }

  // Persist so the agent's next test run sees the change on disk.
  for (const uri of toSave) {
    const document = vscode.workspace.textDocuments.find(
      (candidate) => candidate.uri.fsPath === uri.fsPath
    );
    if (document?.isDirty) {
      await document.save();
    }
  }

  // Returned with the patch result rather than left to a follow-up request. The
  // agent needs to know what its own edit broke, and a separate round trip is one
  // the model has to remember to make.
  await waitForDiagnostics(plan.order);
  return { applied: plan.order, failed: [], diagnostics: collectDiagnostics(plan.order) };
}

function setStatus(state: "connected" | "connecting" | "disconnected"): void {
  const labels = {
    connected: "$(check) Skippy",
    connecting: "$(sync~spin) Skippy",
    disconnected: "$(debug-disconnect) Skippy"
  };
  statusItem.text = labels[state];
  statusItem.tooltip = `Skippy hub: ${state} (${config().serverUrl})`;
  statusItem.show();
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Skippy");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.command = "skippy.status";
  context.subscriptions.push(output, statusItem);

  client = new HubClient(output);

  context.subscriptions.push(
    vscode.commands.registerCommand("skippy.connect", () => client?.connect()),
    vscode.commands.registerCommand("skippy.disconnect", () => client?.disconnect()),
    vscode.commands.registerCommand("skippy.status", () => {
      const state = client?.connected ? "connected" : "disconnected";
      void vscode.window.showInformationMessage(`Skippy hub is ${state} (${config().serverUrl}).`);
      output.show(true);
    })
  );

  setStatus("disconnected");
  if (config().autoConnect) {
    client.connect();
  }
}

export function deactivate(): void {
  client?.disconnect();
  client = undefined;
}
