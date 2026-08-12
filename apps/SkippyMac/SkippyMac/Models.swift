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
    /// Which approval this is within the current run (1-based; 0 = unknown).
    /// A run that writes ten times asks ten times, and without a number the
    /// second card looks like the first one refusing to go away.
    let sequence: Int

    init(
        taskId: String?,
        kind: Kind = .device,
        explanation: String,
        detail: String,
        diff: String = "",
        files: [String] = [],
        sequence: Int = 0
    ) {
        self.id = UUID()
        self.taskId = taskId
        self.kind = kind
        self.explanation = explanation
        self.detail = detail
        self.diff = diff
        self.files = files
        self.sequence = sequence
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
    /// When the last event landed on this card. A slow local model can sit
    /// silent for a minute per turn; the card uses this to say "thinking"
    /// instead of looking frozen.
    var lastEventAt: Date

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
        self.lastEventAt = startedAt
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

/// One project in the hub's memory store, for the chat header's picker.
struct ProjectSummary: Identifiable, Equatable {
    let projectId: String
    let workspaceRoots: [String]
    let sessions: Int
    let updated: String

    var id: String { projectId }

    init(from raw: [String: Any]) {
        projectId = raw["project_id"] as? String ?? ""
        workspaceRoots = raw["workspace_roots"] as? [String] ?? []
        sessions = raw["sessions"] as? Int ?? 0
        updated = raw["updated"] as? String ?? ""
    }
}

/// One past conversation's headline. Tapping it reopens the transcript.
struct ChatSummary: Identifiable, Equatable {
    let chatId: String
    let title: String
    let mode: String
    let updated: String
    let turns: Int

    var id: String { chatId }

    init(from raw: [String: Any]) {
        chatId = raw["chat_id"] as? String ?? ""
        title = raw["title"] as? String ?? ""
        mode = raw["mode"] as? String ?? "chat"
        updated = raw["updated"] as? String ?? ""
        turns = raw["turns"] as? Int ?? 0
    }
}

/// What the hub knows about this project, shaped for the context rail.
struct MemorySnapshot: Equatable {
    let projectId: String
    let conventions: [String: String]
    let decisions: [MemoryDecision]
    let sessions: [MemorySession]
    let chats: [ChatSummary]
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
        chats = (payload["chats"] as? [[String: Any]] ?? []).map { ChatSummary(from: $0) }
    }

    var isEmpty: Bool {
        conventions.isEmpty && decisions.isEmpty && sessions.isEmpty && chats.isEmpty
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

// MARK: - GitHub

/// The hub's GitHub connection: whether a token is stored and who it maps to.
/// The token itself never comes back over the wire — only the login it proved.
struct GitHubStatus: Equatable {
    let connected: Bool
    let login: String
    let name: String
    let error: String?

    init(payload: [String: Any]) {
        connected = payload["connected"] as? Bool ?? false
        login = payload["login"] as? String ?? ""
        name = payload["name"] as? String ?? ""
        error = payload["error"] as? String
    }

    var headline: String {
        if connected { return login.isEmpty ? "Connected" : "Connected as \(login)" }
        return error ?? "Not connected"
    }
}

/// One repository on the connected GitHub account, for the clone picker.
struct GitHubRepo: Identifiable, Equatable {
    let fullName: String
    let name: String
    let isPrivate: Bool
    let detail: String
    let updated: String

    var id: String { fullName }

    init(from raw: [String: Any]) {
        fullName = raw["full_name"] as? String ?? ""
        name = raw["name"] as? String ?? ""
        isPrivate = raw["private"] as? Bool ?? false
        detail = raw["description"] as? String ?? ""
        updated = raw["updated"] as? String ?? ""
    }
}

// MARK: - File explorer

/// One entry of a repo directory listing, as the hub's `files` action reports it.
struct FileEntry: Identifiable, Equatable {
    let name: String
    let isDirectory: Bool
    let size: Int

    var id: String { name }

    init(from raw: [String: Any]) {
        name = raw["name"] as? String ?? ""
        isDirectory = raw["dir"] as? Bool ?? false
        size = raw["size"] as? Int ?? 0
    }

    var sizeText: String {
        guard !isDirectory else { return "" }
        if size < 1024 { return "\(size) B" }
        if size < 1024 * 1024 { return String(format: "%.1f KB", Double(size) / 1024) }
        return String(format: "%.1f MB", Double(size) / (1024 * 1024))
    }
}

/// One directory level of a repo, keyed by the path it answers.
struct FilesListing: Equatable {
    let repo: String
    let path: String
    let entries: [FileEntry]
    let error: String?

    init(payload: [String: Any]) {
        repo = payload["repo"] as? String ?? ""
        path = payload["path"] as? String ?? ""
        entries = (payload["entries"] as? [[String: Any]] ?? []).map { FileEntry(from: $0) }
        error = payload["error"] as? String
    }
}

/// One file's text for the read-only viewer. A binary or oversized file comes
/// back as an error, not as mangled text.
struct FileContent: Equatable {
    let repo: String
    let path: String
    let text: String
    let truncated: Bool
    let error: String?

    init(payload: [String: Any]) {
        repo = payload["repo"] as? String ?? ""
        path = payload["path"] as? String ?? ""
        text = payload["text"] as? String ?? ""
        truncated = payload["truncated"] as? Bool ?? false
        error = payload["error"] as? String
    }
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

/// A wireless bench node the hub has heard from: an IO bridge like the Core2
/// on the bench, with the health it last reported. Every field but `client_id`
/// is best-effort — a node that dropped off still appears, `online: false`,
/// with whatever the hub remembers.
struct BenchNode: Identifiable, Equatable {
    let clientId: String
    /// What a device tool's `host` argument should say to reach this node.
    let host: String
    let name: String
    let firmware: String
    let battery: Int?
    let charging: Bool
    let rssi: Int?
    let ip: String
    let uptimeS: Int?
    let actions: Int
    let busy: Bool
    let uartOpen: Bool
    let ports: [String]
    let online: Bool
    let seenSecondsAgo: Double?

    var id: String { clientId }

    init(from raw: [String: Any]) {
        clientId = raw["client_id"] as? String ?? ""
        let node = raw["node"] as? String ?? ""
        // The hub derives `host` the same way; done here too so a sparse
        // entry (a hello with no node_status yet) still has a usable name.
        let derived = node.isEmpty
            ? (clientId.contains(":")
                ? String(clientId.split(separator: ":", maxSplits: 1)[1])
                : clientId)
            : node
        host = raw["host"] as? String ?? derived
        name = node.isEmpty ? derived : node
        firmware = raw["firmware"] as? String ?? ""
        battery = (raw["battery"] as? NSNumber)?.intValue
        charging = raw["charging"] as? Bool ?? false
        rssi = (raw["rssi"] as? NSNumber)?.intValue
        ip = raw["ip"] as? String ?? ""
        uptimeS = (raw["uptime_s"] as? NSNumber)?.intValue
        actions = (raw["actions"] as? NSNumber)?.intValue ?? 0
        busy = raw["busy"] as? Bool ?? false
        uartOpen = raw["uart_open"] as? Bool ?? false
        ports = raw["ports"] as? [String] ?? []
        online = raw["online"] as? Bool ?? false
        seenSecondsAgo = (raw["seen_seconds_ago"] as? NSNumber)?.doubleValue
    }

    var label: String { name.isEmpty ? clientId : name }

    var detail: String {
        [firmware, ip].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// "100%  -74 dBm  up 5m" — whichever parts the node actually reported.
    var healthSummary: String {
        var parts: [String] = []
        if let battery { parts.append("\(battery)%\(charging ? "+" : "")") }
        if let rssi { parts.append("\(rssi) dBm") }
        if let uptimeS { parts.append("up \(Self.duration(uptimeS))") }
        return parts.joined(separator: "  ")
    }

    /// "last seen 20m ago", for a node that went quiet.
    var lastSeenText: String {
        guard let seenSecondsAgo else { return "last seen: unknown" }
        return "last seen \(Self.duration(Int(seenSecondsAgo))) ago"
    }

    private static func duration(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        return "\(seconds / 3600)h \((seconds % 3600) / 60)m"
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
