import SwiftUI

/// The cockpit: per-mode workspaces (run cards for Code and RE, bubbles for
/// Chat), a composer, and a context rail showing what Skippy already knows
/// about this project.
struct ChatView: View {
    @EnvironmentObject private var app: AppModel
    @State private var showRail = true

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                header
                Divider()
                workspace
                Divider()
                composer
            }
            if showRail {
                Divider()
                ContextRail()
                    .frame(width: 280)
                    .transition(.move(edge: .trailing))
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .animation(.easeInOut(duration: 0.2), value: showRail)
    }

    private var header: some View {
        HStack(spacing: 16) {
            Image(nsImage: NSApplication.shared.applicationIconImage)
                .resizable()
                .frame(width: 28, height: 28)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            Text("Skippy")
                .font(.title2.weight(.semibold))
            Picker("Mode", selection: $app.mode) {
                ForEach(AgentMode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 260)
            .tint(Theme.accent(for: app.mode))
            projectPicker
            if app.mode == .re {
                TextField("RE target (binary / pack key)", text: $app.reTarget)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 260)
                reHostPicker
            }
            Spacer()
            if app.isRunning {
                StatusPill(text: "Running", color: Theme.accent(for: app.mode), pulsing: true)
                Button("Cancel") { app.cancelRun() }
                    .buttonStyle(.bordered)
            }
            Button {
                app.factory.startNewChat()
            } label: {
                Image(systemName: "square.and.pencil")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("New chat — keeps the old one in the rail")
            Button("Clear") { app.factory.clear() }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
            Button {
                showRail.toggle()
            } label: {
                Image(systemName: "sidebar.right")
            }
            .buttonStyle(.plain)
            .foregroundStyle(showRail ? Color.accentColor : .secondary)
            .help(showRail ? "Hide project context" : "Show project context")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    /// Which project's memory this conversation opens with, and where its
    /// transcript files. The options come from the hub — a typed name would
    /// silently create a fresh project.
    private var projectPicker: some View {
        Picker("Project", selection: Binding(
            get: {
                app.factory.selectedProject.isEmpty
                    ? app.factory.defaultProjectId
                    : app.factory.selectedProject
            },
            set: { app.factory.selectProject($0) }
        )) {
            if !app.factory.defaultProjectId.isEmpty {
                Text("\(app.factory.defaultProjectId) (default)")
                    .tag(app.factory.defaultProjectId)
            }
            ForEach(app.factory.projects.filter { $0.projectId != app.factory.defaultProjectId }) { project in
                Text(project.projectId).tag(project.projectId)
            }
        }
        .labelsHidden()
        .frame(maxWidth: 200)
        .help("Which project's memory and chat history this conversation uses.")
        .onAppear { app.factory.requestProjects() }
    }

    /// Which host RE device I/O goes through: local hardware by default, or a
    /// bench node the hub knows about. An offline node stays listed (picking
    /// it is how you find out it went flat), marked as offline.
    private var reHostPicker: some View {
        Picker("Host", selection: $app.reHost) {
            Text("Local devices").tag("")
            ForEach(app.factory.benchNodes) { node in
                Text(node.online ? "Node: \(node.label)" : "Node: \(node.label) (offline)")
                    .tag(node.host)
            }
            // A selection made before the node list arrived still has a row,
            // so the picker never shows blank.
            if !app.reHost.isEmpty,
               !app.factory.benchNodes.contains(where: { $0.host == app.reHost }) {
                Text("Node: \(app.reHost)").tag(app.reHost)
            }
        }
        .labelsHidden()
        .frame(maxWidth: 180)
        .help("Where device I/O runs: this machine, or a wireless bench node.")
        .onAppear { app.factory.requestBridgeNodes() }
    }

    @ViewBuilder
    private var workspace: some View {
        switch app.mode {
        case .chat:
            ChatLane()
        case .coding, .re:
            RunLane(mode: app.mode)
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 12) {
            TextField(placeholder, text: $app.draft, axis: .vertical)
                .lineLimit(1...6)
                .textFieldStyle(.plain)
                .padding(12)
                .background(Color(nsColor: .controlBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .onSubmit { app.send() }
            Button {
                app.send()
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(Theme.accent(for: app.mode))
            }
            .buttonStyle(.plain)
            .disabled(app.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !app.factory.connected)
        }
        .padding(16)
    }

    private var placeholder: String {
        switch app.mode {
        case .coding: return "Ask Skippy to change code…"
        case .re: return "What should Skippy reverse-engineer?"
        case .chat: return "Talk to Skippy…"
        }
    }
}

// MARK: - Chat lane

/// The conversational workspace: plain bubbles, newest at the bottom.
private struct ChatLane: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        if app.factory.chatItems.isEmpty {
            EmptyState(
                icon: "bubble.left.and.bubble.right",
                title: "Just talk",
                message: "Chat has no tools and no sandbox — it is the place to think out loud with Skippy."
            )
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(app.factory.chatItems) { item in
                            TimelineRow(item: item)
                                .id(item.id)
                        }
                    }
                    .padding(20)
                }
                .onChange(of: app.factory.chatItems.count) { _, _ in
                    if let last = app.factory.chatItems.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }
        }
    }
}

// MARK: - Run lane

/// The agent workspace: one card per run, filtered to the selected mode.
private struct RunLane: View {
    @EnvironmentObject private var app: AppModel
    let mode: AgentMode

    private var runs: [RunCard] {
        app.factory.runs.filter { $0.mode == mode }
    }

    var body: some View {
        if runs.isEmpty {
            EmptyState(
                icon: mode == .re ? "cpu" : "hammer",
                title: mode == .re ? "No RE sessions yet" : "No coding runs yet",
                message: mode == .re
                    ? "Point Skippy at a target and each investigation becomes a card here."
                    : "Ask for a change and the run — steps, diffs, outcome — becomes a card here."
            )
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(runs) { run in
                            RunCardView(
                                run: run,
                                // Only the live run can be the one waiting.
                                awaitingApproval: !run.state.isTerminal
                                    && app.factory.pendingApproval != nil,
                                autoApproving: !run.state.isTerminal
                                    && app.factory.autoApproveWrites
                            )
                            .id(run.id)
                        }
                    }
                    .padding(20)
                }
                .onChange(of: runs.count) { _, _ in
                    if let last = runs.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }
        }
    }
}

/// One run: header with state and elapsed time, the outcome when there is one,
/// touched files, and the step-by-step transcript behind a disclosure.
struct RunCardView: View {
    let run: RunCard
    /// The hub is blocked on a human answer for this run.
    var awaitingApproval: Bool = false
    /// Further device writes in this run answer themselves.
    var autoApproving: Bool = false
    @State private var showSteps = false

    private var accent: Color { Theme.accent(for: run.mode) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(run.task)
                    .font(.body.weight(.medium))
                    .textSelection(.enabled)
                Spacer()
                if autoApproving, case .running = run.state {
                    StatusPill(text: "auto-approving writes", color: .orange)
                }
                statePill
            }
            if case .running = run.state {
                activityLine
            }
            HStack(spacing: 8) {
                if case .running = run.state {
                    // Ticks once a second while the run is live.
                    TimelineView(.periodic(from: .now, by: 1)) { _ in
                        Text(run.elapsedText)
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text(run.elapsedText)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                if !run.patchedFiles.isEmpty {
                    Text(run.patchedFiles.joined(separator: "  "))
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            if !run.summary.isEmpty {
                Text(run.summary)
                    .font(.callout)
                    .textSelection(.enabled)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(accent.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            if !run.items.isEmpty {
                DisclosureGroup(isExpanded: $showSteps) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(run.items) { item in
                            TimelineRow(item: item)
                        }
                    }
                    .padding(.top, 6)
                } label: {
                    Text(showSteps ? "Hide steps" : "Show steps (\(run.items.count))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .card(accent: run.state.isTerminal ? .clear : accent)
        .animation(.easeInOut(duration: 0.2), value: run.state)
    }

    /// What the run is doing right now: blocked on the human, or quietly
    /// thinking. A slow local model can go a minute between events, and
    /// without this line that silence reads as a frozen app.
    @ViewBuilder
    private var activityLine: some View {
        if awaitingApproval {
            Label("Waiting for your approval — check the approval window", systemImage: "hand.raised")
                .font(.caption)
                .foregroundStyle(.orange)
        } else {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let quiet = Int(context.date.timeIntervalSince(run.lastEventAt))
                if quiet >= 8 {
                    Label("Thinking — \(quiet)s since the last step", systemImage: "hourglass")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }
        }
    }

    private var statePill: some View {
        switch run.state {
        case .running:
            return StatusPill(
                text: awaitingApproval ? "Needs approval" : "Running",
                color: awaitingApproval ? .orange : accent,
                pulsing: true
            )
        case .finished(let status):
            let good = status.lowercased().contains("complete")
                || status.lowercased().contains("done")
                || status.lowercased().contains("finish")
            return StatusPill(text: status.isEmpty ? "Done" : status, color: good ? .green : .secondary)
        case .failed:
            return StatusPill(text: "Failed", color: .red)
        }
    }
}

// MARK: - Context rail

/// What Skippy already knows about this project: conventions, decisions, and
/// recent sessions, straight from the hub's project memory.
struct ContextRail: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Project context")
                    .font(.headline)
                Spacer()
                Button {
                    app.factory.requestMemory()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Refresh from the hub")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            Divider()
            content
        }
        .background(Theme.railBackground)
    }

    @ViewBuilder
    private var content: some View {
        if let memory = app.factory.memory {
            if let error = memory.error {
                EmptyState(
                    icon: "externaldrive.badge.exclamationmark",
                    title: "Memory unavailable",
                    message: error
                )
            } else if memory.isEmpty {
                EmptyState(
                    icon: "brain",
                    title: "Nothing recorded yet",
                    message: "Runs and decisions land here as project memory builds up."
                )
            } else {
                railBody(memory)
            }
        } else {
            EmptyState(
                icon: "brain",
                title: "Waiting for the hub",
                message: "Project memory loads when the connection is up."
            )
        }
    }

    private func railBody(_ memory: MemorySnapshot) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if !memory.projectId.isEmpty {
                    Label(memory.projectId, systemImage: "folder")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                if !memory.conventions.isEmpty {
                    railSection("Conventions") {
                        ForEach(memory.conventions.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(key)
                                    .font(.caption.weight(.medium))
                                Text(value)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if !memory.chats.isEmpty {
                    railSection("Chats") {
                        ForEach(memory.chats) { chat in
                            ChatRow(chat: chat, current: chat.chatId == app.factory.chatId) {
                                app.factory.openChat(chat.chatId)
                                app.mode = .chat
                            }
                        }
                    }
                }
                if !memory.decisions.isEmpty {
                    railSection("Decisions") {
                        ForEach(memory.decisions.reversed()) { decision in
                            DecisionRow(decision: decision)
                        }
                    }
                }
                if !memory.sessions.isEmpty {
                    railSection("Recent sessions") {
                        ForEach(memory.sessions) { session in
                            SessionRow(session: session)
                        }
                    }
                }
            }
            .padding(16)
        }
    }

    private func railSection<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            content()
        }
    }
}

/// One past conversation. Tapping it reopens the transcript in the chat lane.
private struct ChatRow: View {
    let chat: ChatSummary
    let current: Bool
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    Text(chat.updated.prefix(16).replacingOccurrences(of: "T", with: " "))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    if current {
                        StatusPill(text: "open", color: .green)
                    }
                    if chat.mode != "chat" && !chat.mode.isEmpty {
                        StatusPill(text: chat.mode, color: .secondary)
                    }
                }
                Text(chat.title.isEmpty ? chat.chatId : chat.title)
                    .font(.caption)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                Text("\(chat.turns) turns")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.cardBackground)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(current ? Color.green.opacity(0.4) : .clear, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .help("Reopen this conversation and continue it")
    }
}

private struct DecisionRow: View {
    let decision: MemoryDecision

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(decision.id)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                if decision.superseded {
                    StatusPill(text: "superseded", color: .secondary)
                } else if !decision.stalePaths.isEmpty {
                    StatusPill(text: "may be stale", color: .orange)
                }
            }
            Text(decision.title)
                .font(.caption)
                .strikethrough(decision.superseded, color: .secondary)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct SessionRow: View {
    let session: MemorySession

    private var statusColor: Color {
        let s = session.status.lowercased()
        if s.contains("done") || s.contains("complete") || s.contains("finish") { return .green }
        if s.contains("fail") || s.contains("error") { return .red }
        return .secondary
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                StatusPill(text: session.status.isEmpty ? "?" : session.status, color: statusColor)
                Text(session.recorded)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Text(session.summary.isEmpty ? session.task : session.summary)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(3)
            if !session.filesChanged.isEmpty {
                Text(session.filesChanged.prefix(3).joined(separator: "  "))
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct TimelineRow: View {
    let item: TimelineItem
    @State private var expanded = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            icon
            VStack(alignment: .leading, spacing: 4) {
                Text(item.text)
                    .font(font)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: alignment)
                if !item.detail.isEmpty {
                    DisclosureGroup(isExpanded: $expanded) {
                        Text(item.detail)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .padding(8)
                            .background(Color.primary.opacity(0.05))
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    } label: {
                        Text(expanded ? "Hide detail" : "Show detail")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(12)
        .background(background)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private var icon: some View {
        Group {
            switch item.kind {
            case .user:
                Image(systemName: "person.fill")
            case .thought:
                Image(systemName: "brain")
            case .toolCall(_, let ok):
                Image(systemName: ok == false ? "xmark.circle" : "wrench.and.screwdriver")
            case .patch:
                Image(systemName: "doc.badge.gearshape")
            case .reply:
                Image(systemName: "sparkles")
            case .system:
                Image(systemName: "info.circle")
            case .error:
                Image(systemName: "exclamationmark.triangle")
            case .metrics:
                Image(systemName: "timer")
            }
        }
        .foregroundStyle(tint)
        .frame(width: 18)
    }

    private var tint: Color {
        switch item.kind {
        case .user: return .accentColor
        case .error: return .red
        case .toolCall(_, false?): return .red
        case .toolCall(_, true?): return .green
        case .patch: return .orange
        default: return .secondary
        }
    }

    private var font: Font {
        switch item.kind {
        case .thought, .system: return .callout
        default: return .body
        }
    }

    private var alignment: Alignment {
        item.kind == .user ? .trailing : .leading
    }

    private var background: Color {
        switch item.kind {
        case .user: return Color.accentColor.opacity(0.12)
        case .reply: return Color.primary.opacity(0.04)
        case .error: return Color.red.opacity(0.08)
        default: return Color.clear
        }
    }
}
