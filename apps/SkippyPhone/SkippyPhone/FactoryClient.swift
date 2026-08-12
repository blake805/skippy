import Foundation
import Combine

/// `/ws/factory` client: agent runs, approvals, and (optionally) device-bridge RPC answers.
@MainActor
final class FactoryClient: ObservableObject {
    @Published var connected = false
    @Published var isRunning = false
    @Published var items: [TimelineItem] = []
    @Published var pendingApproval: PendingApproval?
    @Published var statusLine: String = "Disconnected"
    /// What the hub knows about this project, for the memory browser.
    @Published var memory: MemorySnapshot?
    /// Every project in the hub's memory store, and which one the configured
    /// roots default to. "" as a selection means "the default".
    @Published var projects: [ProjectSummary] = []
    @Published var defaultProjectId: String = ""
    @Published var selectedProject: String = ""
    /// The transcript this conversation appends to on the hub. Rotated by
    /// Clear / New chat, replaced when a past chat is reopened.
    @Published private(set) var chatId: String = UUID().uuidString

    private let socket = WebSocketSession()
    private var history: [[String: String]] = []
    private var settings: SettingsStore
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
        // Announce so a reconnect mid-run does not look like a new task, then
        // ask where things stand: a run may have carried on while the phone
        // was in a pocket, and the memory browser wants its data up front.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.socket.sendJSON(["type": "hello", "client": "SkippyPhone"])
            self?.socket.sendJSON(["action": "status"])
            self?.requestMemory()
            self?.requestProjects()
        }
    }

    func requestMemory() {
        var payload: [String: Any] = ["action": "memory"]
        if !selectedProject.isEmpty { payload["project"] = selectedProject }
        socket.sendJSON(payload)
    }

    func requestProjects() {
        socket.sendJSON(["action": "projects"])
    }

    /// Point the chat at another project's memory and chat list. Transcripts
    /// are project-scoped, so the conversation starts over rather than
    /// appending this project's turns to the previous one's transcript.
    func selectProject(_ projectId: String) {
        let chosen = projectId == defaultProjectId ? "" : projectId
        guard chosen != selectedProject else { return }
        selectedProject = chosen
        startNewChat()
        memory = nil
        requestMemory()
    }

    /// Leave the current conversation where it is and open a fresh transcript.
    func startNewChat() {
        items.removeAll()
        history.removeAll()
        chatId = UUID().uuidString
    }

    /// Reopen a past conversation: the hub answers with the full transcript,
    /// which replaces the timeline and seeds the history the next turn sends.
    func openChat(_ chatId: String) {
        var payload: [String: Any] = ["action": "chat_open", "chat_id": chatId]
        if !selectedProject.isEmpty { payload["project"] = selectedProject }
        socket.sendJSON(payload)
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
        items.append(TimelineItem(kind: .user, text: trimmed))
        history.append(["role": "user", "content": trimmed])
        if history.count > 40 {
            history.removeFirst(history.count - 40)
        }

        var payload: [String: Any] = [
            "text": trimmed,
            "mode": mode.wireMode,
            "history": history,
            "chat_id": chatId,
        ]
        if !selectedProject.isEmpty { payload["project"] = selectedProject }
        if mode == .re, !target.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            payload["target"] = target.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        isRunning = true
        statusLine = "Running…"
        socket.sendJSON(payload)
    }

    func cancel() {
        socket.sendJSON(["action": "cancel"])
        items.append(TimelineItem(kind: .system, text: "Cancel requested."))
    }

    func clear() {
        items.removeAll()
        history.removeAll()
        pendingApproval = nil
        chatId = UUID().uuidString
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
        Haptics.tap()
        let noun = pending.kind == .code ? "edit" : "device write"
        let verb = approve ? (approveAll ? "Approved all edits" : "Approved \(noun)")
                           : "Denied \(noun)"
        items.append(TimelineItem(kind: .system, text: "\(verb)."))
        pendingApproval = nil
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
            let task = msg["task"] as? String ?? ""
            items.append(TimelineItem(kind: .system, text: "Started: \(task)"))
        case "agent_thought":
            let content = msg["content"] as? String ?? ""
            if !content.isEmpty {
                items.append(TimelineItem(kind: .thought, text: content))
            }
        case "agent_tool_call":
            let tool = msg["tool"] as? String ?? "tool"
            let args = prettyJSON(msg["args"])
            items.append(TimelineItem(kind: .toolCall(tool: tool, ok: nil), text: tool, detail: args))
        case "agent_tool_result":
            let tool = msg["tool"] as? String ?? "tool"
            let ok = msg["ok"] as? Bool
            let summary = msg["summary"] as? String ?? ""
            let content = msg["content"] as? String ?? ""
            items.append(TimelineItem(
                kind: .toolCall(tool: tool, ok: ok),
                text: summary.isEmpty ? tool : summary,
                detail: content
            ))
        case "agent_patch":
            let files = (msg["files"] as? [[String: Any]] ?? []).compactMap { $0["path"] as? String }
            let diff = msg["diff"] as? String ?? ""
            items.append(TimelineItem(kind: .patch(files: files), text: files.joined(separator: ", "), detail: diff))
        case "agent_done":
            // Status only: the summary arrives right after as the "chat" reply
            // bubble, and printing it here as well showed every outcome twice.
            let status = msg["status"] as? String ?? ""
            items.append(TimelineItem(kind: .system, text: "Done (\(status))"))
        case "chat":
            let content = msg["content"] as? String ?? ""
            if !content.isEmpty {
                items.append(TimelineItem(kind: .reply, text: content))
                history.append(["role": "assistant", "content": content])
            }
        case "done":
            isRunning = false
            statusLine = connected ? "Ready" : "Disconnected"
            // A finished run is new project history; refresh the browser.
            requestMemory()
        case "status":
            // Reconnect answer: the hub says whether our run is still going.
            isRunning = msg["running"] as? Bool ?? false
            if isRunning { statusLine = "Running…" }
        case "memory":
            memory = MemorySnapshot(payload: msg)
        case "projects":
            defaultProjectId = msg["default"] as? String ?? ""
            projects = (msg["projects"] as? [[String: Any]] ?? []).map { ProjectSummary(from: $0) }
        case "chat_open":
            if let error = msg["error"] as? String {
                items.append(TimelineItem(kind: .error, text: error))
            } else if let chat = msg["chat"] as? [String: Any] {
                resumeChat(chat)
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
            // The one event that must be felt: Skippy is waiting on a human.
            Haptics.attention()
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
            Haptics.attention()
        case "error":
            items.append(TimelineItem(kind: .error, text: msg["message"] as? String ?? "Error"))
        default:
            break
        }
    }

    /// Replace the conversation with a reopened transcript.
    private func resumeChat(_ chat: [String: Any]) {
        let turns = chat["turns"] as? [[String: Any]] ?? []
        items = turns.map { turn in
            let role = turn["role"] as? String ?? ""
            let content = turn["content"] as? String ?? ""
            return TimelineItem(kind: role == "user" ? .user : .reply, text: content)
        }
        history = turns.compactMap { turn in
            guard let role = turn["role"] as? String,
                  let content = turn["content"] as? String, !content.isEmpty else { return nil }
            return ["role": role, "content": content]
        }
        if history.count > 40 {
            history.removeFirst(history.count - 40)
        }
        chatId = chat["chat_id"] as? String ?? chatId
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
