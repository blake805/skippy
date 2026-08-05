import SwiftUI

/// The memory browser: what Skippy already knows about the project, in your
/// pocket. Conventions, decisions (with superseded and stale badges), and the
/// session history — the same data as the Mac's context rail, shaped for a
/// phone's single column.
struct MemoryView: View {
    @ObservedObject var factory: FactoryClient

    var body: some View {
        NavigationStack {
            Group {
                if let memory = factory.memory, !memory.isEmpty {
                    content(memory)
                } else if let error = factory.memory?.error {
                    EmptyState(
                        icon: "brain",
                        title: "Memory unavailable",
                        message: error
                    )
                } else {
                    EmptyState(
                        icon: "brain",
                        title: "Nothing remembered yet",
                        message: "Skippy records conventions, decisions, and session history as it works. They will appear here."
                    )
                }
            }
            .navigationTitle(title)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        factory.requestMemory()
                        Haptics.tap()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
        }
    }

    private var title: String {
        let project = factory.memory?.projectId ?? ""
        return project.isEmpty || project == "unscoped" ? "Memory" : project
    }

    private func content(_ memory: MemorySnapshot) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if !memory.conventions.isEmpty {
                    conventions(memory.conventions)
                }
                if !memory.decisions.isEmpty {
                    decisions(memory.decisions)
                }
                if !memory.sessions.isEmpty {
                    sessions(memory.sessions)
                }
            }
            .padding(16)
        }
        .refreshable {
            factory.requestMemory()
        }
    }

    private func conventions(_ items: [String: String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionLabel("Conventions", icon: "text.book.closed")
            ForEach(items.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(key)
                        .font(.system(.caption, design: .monospaced).weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(value)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .card()
    }

    private func decisions(_ items: [MemoryDecision]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionLabel("Decisions", icon: "signpost.right")
            ForEach(items) { decision in
                VStack(alignment: .leading, spacing: 4) {
                    Text(decision.title)
                        .font(.callout.weight(.medium))
                        .strikethrough(decision.superseded)
                        .foregroundStyle(decision.superseded ? .secondary : .primary)
                    HStack(spacing: 6) {
                        if decision.superseded {
                            StatusPill(text: "superseded", color: .secondary)
                        }
                        if !decision.stalePaths.isEmpty {
                            StatusPill(text: "may be stale", color: .orange)
                        }
                        Text(decision.recorded.prefix(10))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                if decision.id != items.last?.id {
                    Divider()
                }
            }
        }
        .card()
    }

    private func sessions(_ items: [MemorySession]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionLabel("Sessions", icon: "clock.arrow.circlepath")
            ForEach(items) { session in
                SessionRow(session: session)
                if session.id != items.last?.id {
                    Divider()
                }
            }
        }
        .card()
    }

    private func sectionLabel(_ title: String, icon: String) -> some View {
        Label(title, systemImage: icon)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.secondary)
    }
}

private struct SessionRow: View {
    let session: MemorySession
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                StatusPill(
                    text: session.status.isEmpty ? "?" : session.status,
                    color: session.status == "done" ? .green : .orange
                )
                if !session.mode.isEmpty {
                    Text(session.mode)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                Spacer()
                Text(session.recorded.prefix(10))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Text(session.task)
                .font(.callout)
                .lineLimit(expanded ? nil : 2)
            if expanded {
                if !session.summary.isEmpty {
                    Text(session.summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !session.filesChanged.isEmpty {
                    Text(session.filesChanged.joined(separator: "  "))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
        }
    }
}
