import SwiftUI

struct ChatView: View {
    @EnvironmentObject private var app: AppModel
    @FocusState private var composerFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            timeline
            Divider()
            composer
        }
        .background(Color(.systemBackground))
    }

    private var header: some View {
        VStack(spacing: 8) {
            HStack(spacing: 10) {
                Text("Skippy")
                    .font(.title3.weight(.semibold))
                Circle()
                    .fill(app.factory.connected ? Color.green : Color.orange)
                    .frame(width: 8, height: 8)
                Text(app.factory.statusLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                if app.isRunning {
                    Button("Cancel") { app.cancelRun() }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
                Button {
                    app.factory.startNewChat()
                } label: {
                    Image(systemName: "square.and.pencil")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                Button("Clear") { app.factory.clear() }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
            }
            Picker("Mode", selection: $app.mode) {
                ForEach(AgentMode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            projectPicker
            if app.mode == .re {
                TextField("RE target (binary / pack key)", text: $app.reTarget)
                    .textFieldStyle(.roundedBorder)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    /// Which project's memory this conversation opens with, and where its
    /// transcript files. Options come from the hub — a typed name would
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
        .pickerStyle(.menu)
        .onAppear { app.factory.requestProjects() }
    }

    private var timeline: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(app.factory.items) { item in
                        TimelineRow(item: item)
                            .id(item.id)
                    }
                }
                .padding(16)
            }
            .onChange(of: app.factory.items.count) { _, _ in
                if let last = app.factory.items.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
            .onTapGesture { composerFocused = false }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 12) {
            TextField(placeholder, text: $app.draft, axis: .vertical)
                .lineLimit(1...6)
                .textFieldStyle(.plain)
                .focused($composerFocused)
                .padding(12)
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .onSubmit { app.send() }
            Button {
                app.send()
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 28))
            }
            .buttonStyle(.plain)
            .disabled(app.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !app.factory.connected)
        }
        .padding(12)
    }

    private var placeholder: String {
        switch app.mode {
        case .coding: return "Ask Skippy to change code…"
        case .re: return "What should Skippy reverse-engineer?"
        case .chat: return "Talk to Skippy…"
        }
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
