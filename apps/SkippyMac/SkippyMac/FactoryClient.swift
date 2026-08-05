import Foundation
import Combine

/// `/ws/factory` client: agent runs, approvals, and (optionally) device-bridge RPC answers.
@MainActor
final class FactoryClient: ObservableObject {
    @Published var connected = false
    @Published var isRunning = false
    /// Agent runs, oldest first. Coding and RE work lands here as cards.
    @Published var runs: [RunCard] = []
    /// The conversational lane: plain bubbles, no run lifecycle.
    @Published var chatItems: [TimelineItem] = []
    /// What the hub knows about this project, for the context rail.
    @Published var memory: MemorySnapshot?
    /// RE note packs (list view) and the findings of the open pack.
    @Published var rePacks: [REPackSummary] = []
    @Published var reFindings: [REFinding] = []
    @Published var reOpenPackId: String = ""
    @Published var reStudioDevices: [REDevice] = []
    @Published var reNotice: String = ""
    /// The repo panel: every repo's headline, the open repo in full, and the
    /// last commit/branch action's outcome.
    @Published var gitRepos: [GitRepoSummary] = []
    @Published var gitDetail: GitRepoDetail?
    @Published var gitSelectedRepo: String = ""
    @Published var gitNotice: String = ""
    @Published var pendingApproval: PendingApproval?
    @Published var statusLine: String = "Disconnected"

    private let socket = WebSocketSession()
    private var history: [[String: String]] = []
    private var settings: SettingsStore
    /// The run currently receiving events. One task per client is the hub's
    /// contract, so a single pointer is enough.
    private var activeRunId: UUID?
    /// Optional handler for device_* RPC actions when this socket is the bridge.
    var onDeviceRPC: (([String: Any]) async -> [String: Any])?

    init(settings: SettingsStore) {
        self.settings = settings
        socket.onStateChange = { [weak self] ok in
            Task { @MainActor in
                self?.connected = ok
                self?.statusLine = ok ? "Connected" : "Reconnecting…"
            }
        }
        socket.onMessage = { [weak self] msg in
            Task { @MainActor in self?.handle(msg) }
        }
    }

    func connect() {
        socket.connect(to: settings.factoryURL)
        // Announce so a reconnect mid-run does not look like a new task, then ask
        // where things stand: a run may have carried on while we were away, and
        // the context rail wants the project memory up front.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.socket.sendJSON(["type": "hello", "client": "SkippyMac"])
            self?.socket.sendJSON(["action": "status"])
            self?.socket.sendJSON(["action": "memory"])
        }
    }

    func requestMemory() {
        socket.sendJSON(["action": "memory"])
    }

    // MARK: - RE dashboard queries

    func requestPacks() {
        socket.sendJSON(["action": "re_notes"])
    }

    func requestPack(_ packId: String) {
        socket.sendJSON(["action": "re_notes", "pack_id": packId])
    }

    func requestStudioDevices() {
        socket.sendJSON(["action": "re_devices", "host": "studio"])
    }

    func addFinding(_ fields: [String: Any]) {
        var payload = fields
        payload["action"] = "re_add_finding"
        socket.sendJSON(payload)
    }

    // MARK: - Repo panel queries

    func requestGitRepos() {
        socket.sendJSON(["action": "git"])
    }

    func requestGitDetail(_ repo: String) {
        gitSelectedRepo = repo
        socket.sendJSON(["action": "git", "repo": repo])
    }

    /// Commit from the panel. The human wrote the message and clicked the
    /// button, so this writes directly — the approval card is for the agent.
    func gitCommit(repo: String, message: String) {
        socket.sendJSON(["action": "git_commit", "repo": repo, "message": message])
    }

    func gitBranch(repo: String, name: String, create: Bool) {
        socket.sendJSON(["action": "git_branch", "repo": repo, "name": name, "create": create])
    }

    private func refreshGit() {
        requestGitRepos()
        if !gitSelectedRepo.isEmpty {
            requestGitDetail(gitSelectedRepo)
        }
    }

    func disconnect() {
        socket.disconnect()
    }

    func updateSettings(_ settings: SettingsStore) {
        self.settings = settings
    }

    func send(text: String, mode: AgentMode, target: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if mode == .chat {
            chatItems.append(TimelineItem(kind: .user, text: trimmed))
        } else {
            let card = RunCard(task: trimmed, mode: mode)
            runs.append(card)
            activeRunId = card.id
        }
        history.append(["role": "user", "content": trimmed])
        if history.count > 40 {
            history.removeFirst(history.count - 40)
        }

        var payload: [String: Any] = [
            "text": trimmed,
            "mode": mode.wireMode,
            "history": history,
        ]
        if mode == .re, !target.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            payload["target"] = target.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        isRunning = true
        statusLine = "Running…"
        socket.sendJSON(payload)
    }

    func cancel() {
        socket.sendJSON(["action": "cancel"])
        appendToRun(TimelineItem(kind: .system, text: "Cancel requested."))
    }

    func clear() {
        runs.removeAll()
        chatItems.removeAll()
        activeRunId = nil
        history.removeAll()
        pendingApproval = nil
    }

    /// Answer the pending approval. `approveAll` (code edits only) tells the hub
    /// to stop asking for the rest of this run.
    func respondToApproval(approve: Bool, approveAll: Bool = false) {
        guard let pending = pendingApproval else { return }
        var payload: [String: Any] = ["status": approve ? "APPROVE" : "DENY"]
        if approve, approveAll { payload["scope"] = "all" }
        if let taskId = pending.taskId, !taskId.isEmpty {
            payload["task_id"] = taskId
        }
        socket.sendJSON(payload)
        let noun = pending.kind == .code ? "edit" : "device write"
        let verb = approve ? (approveAll ? "Approved all edits" : "Approved \(noun)")
                           : "Denied \(noun)"
        appendToRun(TimelineItem(kind: .system, text: "\(verb)."))
        pendingApproval = nil
    }

    /// Route an event into the active run's card, or the chat lane when no
    /// agent run is receiving events.
    private func appendToRun(_ item: TimelineItem) {
        if let id = activeRunId, let index = runs.firstIndex(where: { $0.id == id }) {
            runs[index].items.append(item)
        } else {
            chatItems.append(item)
        }
    }

    private func mutateActiveRun(_ change: (inout RunCard) -> Void) {
        guard let id = activeRunId, let index = runs.firstIndex(where: { $0.id == id }) else { return }
        change(&runs[index])
    }

    private func handle(_ msg: [String: Any]) {
        // Device-bridge RPCs (and Cursor-style actions) carry `action` + `task_id`.
        if let action = msg["action"] as? String, let taskId = msg["task_id"] as? String {
            if action.hasPrefix("device_") {
                Task { await answerDeviceRPC(msg, taskId: taskId) }
                return
            }
        }

        let type = msg["type"] as? String ?? ""
        switch type {
        case "agent_start":
            isRunning = true
        case "agent_thought":
            let content = msg["content"] as? String ?? ""
            if !content.isEmpty {
                appendToRun(TimelineItem(kind: .thought, text: content))
            }
        case "agent_tool_call":
            let tool = msg["tool"] as? String ?? "tool"
            let args = prettyJSON(msg["args"])
            appendToRun(TimelineItem(kind: .toolCall(tool: tool, ok: nil), text: tool, detail: args))
        case "agent_tool_result":
            let tool = msg["tool"] as? String ?? "tool"
            let ok = msg["ok"] as? Bool
            let summary = msg["summary"] as? String ?? ""
            let content = msg["content"] as? String ?? ""
            appendToRun(TimelineItem(
                kind: .toolCall(tool: tool, ok: ok),
                text: summary.isEmpty ? tool : summary,
                detail: content
            ))
        case "agent_patch":
            let files = (msg["files"] as? [[String: Any]] ?? []).compactMap { $0["path"] as? String }
            let diff = msg["diff"] as? String ?? ""
            appendToRun(TimelineItem(kind: .patch(files: files), text: files.joined(separator: ", "), detail: diff))
            mutateActiveRun { card in
                for file in files where !card.patchedFiles.contains(file) {
                    card.patchedFiles.append(file)
                }
            }
        case "agent_done":
            let status = msg["status"] as? String ?? ""
            mutateActiveRun { card in
                card.state = .finished(status: status)
                card.endedAt = Date()
            }
        case "chat":
            let content = msg["content"] as? String ?? ""
            if !content.isEmpty {
                // During an agent run this is the outcome summary; it belongs on
                // the card, not in the conversation lane.
                if activeRunId != nil {
                    mutateActiveRun { $0.summary = content }
                } else {
                    chatItems.append(TimelineItem(kind: .reply, text: content))
                }
                history.append(["role": "assistant", "content": content])
            }
        case "done":
            isRunning = false
            statusLine = connected ? "Ready" : "Disconnected"
            mutateActiveRun { card in
                if !card.state.isTerminal {
                    // A chat-lane refusal ("still working on the previous
                    // request") or an internal error ends without agent_done.
                    card.state = .finished(status: card.summary.isEmpty ? "ended" : "done")
                    card.endedAt = Date()
                }
            }
            activeRunId = nil
            // A finished run is new project history; refresh the rail. And it
            // may have changed or committed files, so refresh the repo panel
            // when it has been opened at least once.
            requestMemory()
            if !gitRepos.isEmpty { refreshGit() }
        case "status":
            // Reconnect answer: the hub says whether our run is still going.
            isRunning = msg["running"] as? Bool ?? false
            if isRunning { statusLine = "Running…" }
        case "memory":
            memory = MemorySnapshot(payload: msg)
        case "re_notes":
            if let error = msg["error"] as? String {
                reNotice = error
            } else if let packs = msg["packs"] as? [[String: Any]] {
                rePacks = packs.map { REPackSummary(from: $0) }
            } else {
                reOpenPackId = msg["pack_id"] as? String ?? ""
                reFindings = (msg["findings"] as? [[String: Any]] ?? []).map { REFinding(from: $0) }
            }
        case "re_devices":
            if let error = msg["error"] as? String {
                reNotice = error
            } else {
                reStudioDevices = (msg["devices"] as? [[String: Any]] ?? []).map { REDevice(from: $0) }
            }
        case "git":
            if let repos = msg["repos"] as? [[String: Any]] {
                gitRepos = repos.map { GitRepoSummary(from: $0) }
                // First answer picks the open repo, so the panel is never a
                // list with nothing selected.
                if gitSelectedRepo.isEmpty, let first = gitRepos.first {
                    requestGitDetail(first.name)
                }
            } else if let error = msg["error"] as? String {
                gitNotice = error
            } else {
                gitDetail = GitRepoDetail(payload: msg)
            }
        case "git_result":
            if let error = msg["error"] as? String {
                gitNotice = error
            } else {
                gitNotice = msg["summary"] as? String ?? "Done."
                refreshGit()
            }
        case "re_finding_saved":
            if let error = msg["error"] as? String {
                reNotice = "Could not save finding: \(error)"
            } else {
                reNotice = msg["summary"] as? String ?? "Finding saved."
                requestPacks()
                let pid = msg["pack_id"] as? String ?? reOpenPackId
                if !pid.isEmpty { requestPack(pid) }
            }
        case "code_auth":
            let explanation = msg["explanation"] as? String ?? "Skippy wants to change your files."
            let diff = msg["diff"] as? String ?? ""
            let files = (msg["files"] as? [[String: Any]] ?? []).compactMap { $0["path"] as? String }
            pendingApproval = PendingApproval(
                taskId: msg["task_id"] as? String,
                kind: .code,
                explanation: explanation,
                detail: diff,
                diff: diff,
                files: files
            )
        case "device_auth", "terminal_auth", "deployment_auth":
            let explanation = msg["explanation"] as? String
                ?? msg["command"] as? String
                ?? "Authorization required"
            let detail = prettyJSON(msg)
            pendingApproval = PendingApproval(
                taskId: msg["task_id"] as? String,
                kind: .device,
                explanation: explanation,
                detail: detail
            )
        case "error":
            appendToRun(TimelineItem(kind: .error, text: msg["message"] as? String ?? "Error"))
        default:
            break
        }
    }

    private func answerDeviceRPC(_ msg: [String: Any], taskId: String) async {
        guard let onDeviceRPC else {
            socket.sendJSON([
                "task_id": taskId,
                "ok": false,
                "error": "This client is not acting as a device bridge.",
            ])
            return
        }
        let reply = await onDeviceRPC(msg)
        var out = reply
        out["task_id"] = taskId
        socket.sendJSON(out)
    }

    private func prettyJSON(_ value: Any?) -> String {
        guard let value else { return "" }
        if let s = value as? String { return s }
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8) else {
            return String(describing: value)
        }
        return text
    }
}
