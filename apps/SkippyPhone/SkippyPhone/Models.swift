import Foundation

enum AgentMode: String, CaseIterable, Identifiable {
    case coding = "Code"
    case re = "RE"
    case chat = "Chat"

    var id: String { rawValue }

    /// Wire value the hub understands. Chat is its own lane on the hub — a
    /// conversational turn, not an agent run — so it must not map to "Agent".
    var wireMode: String {
        switch self {
        case .coding: return "Agent"
        case .re: return "RE"
        case .chat: return "Chat"
        }
    }
}

enum SidebarPage: String, CaseIterable, Identifiable {
    case work = "Work"
    case voice = "Voice"
    case memory = "Memory"
    case settings = "Settings"

    var id: String { rawValue }

    var systemImage: String {
        switch self {
        case .work: return "bubble.left.and.bubble.right"
        case .voice: return "waveform"
        case .memory: return "brain"
        case .settings: return "gearshape"
        }
    }
}

/// One row in the conversation timeline.
struct TimelineItem: Identifiable, Equatable {
    enum Kind: Equatable {
        case user
        case thought
        case toolCall(tool: String, ok: Bool?)
        case patch(files: [String])
        case reply
        case system
        case error
        case metrics
    }

    let id: UUID
    let kind: Kind
    var text: String
    var detail: String
    let createdAt: Date

    init(kind: Kind, text: String, detail: String = "", id: UUID = UUID(), createdAt: Date = Date()) {
        self.id = id
        self.kind = kind
        self.text = text
        self.detail = detail
        self.createdAt = createdAt
    }
}

struct PendingApproval: Identifiable, Equatable {
    /// What is being approved. A code edit gets the diff viewer and an
    /// "Approve all" affordance; a device write is a one-off, so it does not.
    enum Kind: Equatable {
        case code
        case device
    }

    let id: UUID
    let taskId: String?
    let kind: Kind
    let explanation: String
    /// Raw fallback text (the pretty-printed payload) for device writes.
    let detail: String
    /// Unified diff for a code edit; empty for a device write.
    let diff: String
    /// File paths a code edit touches; empty for a device write.
    let files: [String]

    init(
        taskId: String?,
        kind: Kind = .device,
        explanation: String,
        detail: String,
        diff: String = "",
        files: [String] = []
    ) {
        self.id = UUID()
        self.taskId = taskId
        self.kind = kind
        self.explanation = explanation
        self.detail = detail
        self.diff = diff
        self.files = files
    }
}

/// A decision from project memory, as the memory browser shows it.
struct MemoryDecision: Identifiable, Equatable {
    let id: String
    let title: String
    let recorded: String
    let superseded: Bool
    let stalePaths: [String]
}

/// A past session from project memory.
struct MemorySession: Identifiable, Equatable {
    let id: String
    let recorded: String
    let status: String
    let task: String
    let summary: String
    let filesChanged: [String]
    let mode: String
}

/// What the hub knows about this project, shaped for the memory browser.
/// Identical parsing to the Mac's context rail, so the two stay in step.
struct MemorySnapshot: Equatable {
    let projectId: String
    let conventions: [String: String]
    let decisions: [MemoryDecision]
    let sessions: [MemorySession]
    let error: String?

    /// Parse the hub's `{"type": "memory", ...}` payload. Tolerant of missing
    /// fields: an older hub or an errored snapshot still yields something.
    init(payload: [String: Any]) {
        projectId = payload["project_id"] as? String ?? ""
        error = payload["error"] as? String
        conventions = (payload["conventions"] as? [String: String])
            ?? (payload["conventions"] as? [String: Any])?.compactMapValues { $0 as? String }
            ?? [:]
        decisions = (payload["decisions"] as? [[String: Any]] ?? []).map { raw in
            MemoryDecision(
                id: raw["id"] as? String ?? "",
                title: raw["title"] as? String ?? "",
                recorded: raw["recorded"] as? String ?? "",
                superseded: raw["superseded"] as? Bool ?? false,
                stalePaths: raw["stale_paths"] as? [String] ?? []
            )
        }
        sessions = (payload["sessions"] as? [[String: Any]] ?? []).map { raw in
            MemorySession(
                id: raw["session_id"] as? String ?? "",
                recorded: raw["recorded"] as? String ?? "",
                status: raw["status"] as? String ?? "",
                task: raw["task"] as? String ?? "",
                summary: raw["summary"] as? String ?? "",
                filesChanged: raw["files_changed"] as? [String] ?? [],
                mode: raw["mode"] as? String ?? ""
            )
        }
    }

    var isEmpty: Bool {
        conventions.isEmpty && decisions.isEmpty && sessions.isEmpty
    }
}

struct VoiceMetrics: Equatable {
    var sttMs: Int = 0
    var firstTokenMs: Int = 0
    var firstAudioMs: Int = 0
    var totalMs: Int = 0
}
