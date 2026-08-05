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
    case reverse = "Reverse Engineer"
    case repo = "Repo"
    case voice = "Voice"
    case settings = "Settings"

    var id: String { rawValue }

    var systemImage: String {
        switch self {
        case .work: return "bubble.left.and.bubble.right"
        case .reverse: return "cpu"
        case .repo: return "arrow.triangle.branch"
        case .voice: return "waveform"
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

/// The lifecycle of one agent run, as the cockpit shows it.
enum RunState: Equatable {
    case running
    case finished(status: String)
    case failed

    var isTerminal: Bool {
        if case .running = self { return false }
        return true
    }
}

/// One agent run as a card: the task, its state, elapsed time, and everything
/// that happened inside it. This replaces the flat log — a background run is a
/// thing with a beginning and an end, not a region of scrollback.
struct RunCard: Identifiable, Equatable {
    let id: UUID
    let task: String
    let mode: AgentMode
    let startedAt: Date
    var endedAt: Date?
    var state: RunState
    var items: [TimelineItem]
    var summary: String
    var patchedFiles: [String]

    init(task: String, mode: AgentMode, id: UUID = UUID(), startedAt: Date = Date()) {
        self.id = id
        self.task = task
        self.mode = mode
        self.startedAt = startedAt
        self.endedAt = nil
        self.state = .running
        self.items = []
        self.summary = ""
        self.patchedFiles = []
    }

    var elapsed: TimeInterval {
        (endedAt ?? Date()).timeIntervalSince(startedAt)
    }

    var elapsedText: String {
        let seconds = Int(elapsed.rounded())
        if seconds < 60 { return "\(seconds)s" }
        return "\(seconds / 60)m \(seconds % 60)s"
    }
}

/// A decision from project memory, as the context rail shows it.
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

/// What the hub knows about this project, shaped for the context rail.
struct MemorySnapshot: Equatable {
    let projectId: String
    let conventions: [String: String]
    let decisions: [MemoryDecision]
    let sessions: [MemorySession]
    let error: String?

    /// Parse the hub's `{"type": "memory", ...}` payload. Tolerant of missing
    /// fields: an older hub or an errored snapshot still yields something the
    /// rail can render.
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

// MARK: - Repo panel

/// One changed file, as `git status --porcelain` reports it.
struct GitChange: Identifiable, Equatable {
    let status: String
    let path: String

    var id: String { path }

    /// Untracked files show as "??"; everything else keeps git's letter.
    var badge: String { status == "??" ? "new" : status }

    init(from raw: [String: Any]) {
        status = raw["status"] as? String ?? ""
        path = raw["path"] as? String ?? ""
    }
}

/// The last commit on a branch, for the repo header.
struct GitCommitInfo: Equatable {
    let hash: String
    let subject: String
    let when: String
    let author: String

    var isEmpty: Bool { hash.isEmpty }

    init(from raw: [String: Any]) {
        hash = raw["hash"] as? String ?? ""
        subject = raw["subject"] as? String ?? ""
        when = raw["when"] as? String ?? ""
        author = raw["author"] as? String ?? ""
    }
}

/// One repository's headline in the repo list.
struct GitRepoSummary: Identifiable, Equatable {
    let name: String
    let branch: String
    let changes: Int
    let ahead: Int
    let behind: Int
    let lastCommit: GitCommitInfo

    var id: String { name }

    init(from raw: [String: Any]) {
        name = raw["name"] as? String ?? ""
        branch = raw["branch"] as? String ?? ""
        changes = raw["changes"] as? Int ?? 0
        ahead = raw["ahead"] as? Int ?? 0
        behind = raw["behind"] as? Int ?? 0
        lastCommit = GitCommitInfo(from: raw["last_commit"] as? [String: Any] ?? [:])
    }
}

/// One repository in full: what the repo panel renders.
struct GitRepoDetail: Equatable {
    let repo: String
    let branch: String
    let ahead: Int
    let behind: Int
    let changes: [GitChange]
    let branches: [String]
    let lastCommit: GitCommitInfo
    /// Unified diff of the working tree against HEAD.
    let diff: String
    /// Unified diff of the index (what a commit would include right now).
    let stagedDiff: String
    let untracked: [String]
    let error: String?

    /// Parse the hub's `{"type": "git", "repo": ...}` payload. Tolerant of
    /// missing fields so an older hub still renders something.
    init(payload: [String: Any]) {
        repo = payload["repo"] as? String ?? ""
        branch = payload["branch"] as? String ?? ""
        ahead = payload["ahead"] as? Int ?? 0
        behind = payload["behind"] as? Int ?? 0
        changes = (payload["changes"] as? [[String: Any]] ?? []).map { GitChange(from: $0) }
        branches = payload["branches"] as? [String] ?? []
        lastCommit = GitCommitInfo(from: payload["last_commit"] as? [String: Any] ?? [:])
        diff = payload["diff"] as? String ?? ""
        stagedDiff = payload["staged_diff"] as? String ?? ""
        untracked = payload["untracked"] as? [String] ?? []
        error = payload["error"] as? String
    }

    var isClean: Bool { changes.isEmpty }
}

// MARK: - Reverse engineering

/// A target Skippy can talk to: a serial port, a USB device, or a network
/// endpoint, on this Mac ("macbook") or the hub ("studio").
struct REDevice: Identifiable, Equatable {
    enum Kind: String { case serial, usb, net }

    let id: String
    let kind: Kind
    let host: String
    let label: String
    let detail: String
    /// The serial port path, for opening a session.
    let port: String
    let vid: String
    let pid: String

    var isLocal: Bool { host == "macbook" }

    init(from raw: [String: Any]) {
        let kindRaw = raw["kind"] as? String ?? "serial"
        kind = Kind(rawValue: kindRaw) ?? .serial
        host = raw["host"] as? String ?? "studio"
        port = raw["port"] as? String ?? ""
        vid = raw["vid"] as? String ?? ""
        pid = raw["pid"] as? String ?? ""
        let description = raw["description"] as? String ?? ""
        let product = raw["product"] as? String ?? ""
        let manufacturer = raw["manufacturer"] as? String ?? ""
        switch kind {
        case .serial:
            label = port.isEmpty ? description : port
            detail = description
        case .usb:
            label = "\(vid):\(pid)"
            detail = [manufacturer, product].filter { !$0.isEmpty }.joined(separator: " ")
        case .net:
            label = port
            detail = description
        }
        id = "\(host)/\(kindRaw)/\(port.isEmpty ? "\(vid):\(pid)" : port)"
    }

    /// A synthetic entry for a network target the user typed in.
    init(netAddress: String, port: Int) {
        kind = .net
        host = "macbook"
        self.port = "\(netAddress):\(port)"
        vid = ""; pid = ""
        label = "\(netAddress):\(port)"
        detail = "tcp"
        id = "macbook/net/\(netAddress):\(port)"
    }
}

/// One frame in the traffic console: which way it went, when, and the bytes.
struct TrafficFrame: Identifiable, Equatable {
    enum Direction { case tx, rx, note }

    let id = UUID()
    let direction: Direction
    let timestamp: Date
    let bytes: [UInt8]
    let note: String

    init(direction: Direction, bytes: [UInt8] = [], note: String = "", timestamp: Date = Date()) {
        self.direction = direction
        self.bytes = bytes
        self.note = note
        self.timestamp = timestamp
    }
}

/// One RE finding as the notebook shows it, straight from a note pack.
struct REFinding: Identifiable, Equatable {
    let id: String
    let kind: String
    let title: String
    let confidence: String
    let location: String
    let recorded: String
    let superseded: Bool
    let text: String

    init(from raw: [String: Any]) {
        id = raw["id"] as? String ?? ""
        kind = raw["kind"] as? String ?? ""
        title = raw["title"] as? String ?? ""
        confidence = raw["confidence"] as? String ?? ""
        location = raw["location"] as? String ?? ""
        recorded = raw["recorded"] as? String ?? ""
        superseded = raw["superseded"] as? Bool ?? false
        text = raw["text"] as? String ?? ""
    }
}

/// A note pack in the list, keyed by target.
struct REPackSummary: Identifiable, Equatable {
    let id: String
    let target: String
    let title: String
    let findings: Int
    let updated: String

    init(from raw: [String: Any]) {
        id = raw["pack_id"] as? String ?? ""
        target = raw["target"] as? String ?? ""
        title = raw["title"] as? String ?? ""
        findings = raw["findings"] as? Int ?? 0
        updated = raw["updated"] as? String ?? ""
    }
}

/// Hex + ASCII dump utilities. Kept here (not in a view) so they can be tested.
enum HexDump {
    /// Parse a loose hex string — spaces, newlines, and 0x prefixes tolerated.
    static func parse(_ input: String) -> [UInt8]? {
        var s = input.lowercased()
        s = s.replacingOccurrences(of: "0x", with: "")
        let filtered = s.filter { $0.isHexDigit }
        guard filtered.count % 2 == 0 else { return nil }
        var bytes: [UInt8] = []
        var index = filtered.startIndex
        while index < filtered.endIndex {
            let next = filtered.index(index, offsetBy: 2)
            guard let byte = UInt8(filtered[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        return bytes
    }

    static func hex(_ bytes: [UInt8]) -> String {
        bytes.map { String(format: "%02x", $0) }.joined(separator: " ")
    }

    static func ascii(_ bytes: [UInt8]) -> String {
        String(bytes.map { $0 >= 0x20 && $0 < 0x7f ? Character(UnicodeScalar($0)) : "." })
    }

    /// Classic 16-byte-per-row `offset  hex  |ascii|` layout.
    static func dump(_ bytes: [UInt8]) -> String {
        guard !bytes.isEmpty else { return "" }
        var out: [String] = []
        var offset = 0
        while offset < bytes.count {
            let row = Array(bytes[offset..<min(offset + 16, bytes.count)])
            let hexPart = row.map { String(format: "%02x", $0) }
                .joined(separator: " ")
                .padding(toLength: 16 * 3 - 1, withPad: " ", startingAt: 0)
            out.append(String(format: "%04x  %@  |%@|", offset, hexPart, ascii(row)))
            offset += 16
        }
        return out.joined(separator: "\n")
    }

    /// Byte offsets where two buffers differ — what the scratchpad highlights
    /// when comparing two responses.
    static func diffOffsets(_ a: [UInt8], _ b: [UInt8]) -> Set<Int> {
        var offsets = Set<Int>()
        let count = max(a.count, b.count)
        for i in 0..<count {
            let av = i < a.count ? a[i] : nil
            let bv = i < b.count ? b[i] : nil
            if av != bv { offsets.insert(i) }
        }
        return offsets
    }
}

struct VoiceMetrics: Equatable {
    var sttMs: Int = 0
    var firstTokenMs: Int = 0
    var firstAudioMs: Int = 0
    var totalMs: Int = 0
}
