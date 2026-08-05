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
        // Announce so a reconnect mid-run does not look like a new task.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.socket.sendJSON(["type": "hello", "client": "SkippyMac"])
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
        items.append(TimelineItem(kind: .user, text: trimmed))
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
        items.append(TimelineItem(kind: .system, text: "Cancel requested."))
    }

    func clear() {
        items.removeAll()
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
            items.append(TimelineItem(kind: .error, text: msg["message"] as? String ?? "Error"))
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
