import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        TabView(selection: Binding(
            get: { app.page },
            set: { newPage in
                app.page = newPage
                if newPage == .voice { app.openVoice() }
                if newPage == .memory { app.factory.requestMemory() }
            }
        )) {
            ChatView()
                .tabItem { Label(SidebarPage.work.rawValue, systemImage: SidebarPage.work.systemImage) }
                .tag(SidebarPage.work)
            VoiceView(voice: app.voice)
                .tabItem { Label(SidebarPage.voice.rawValue, systemImage: SidebarPage.voice.systemImage) }
                .tag(SidebarPage.voice)
            MemoryView(factory: app.factory)
                .environmentObject(app)
                .tabItem { Label(SidebarPage.memory.rawValue, systemImage: SidebarPage.memory.systemImage) }
                .tag(SidebarPage.memory)
            SettingsView()
                .tabItem { Label(SidebarPage.settings.rawValue, systemImage: SidebarPage.settings.systemImage) }
                .tag(SidebarPage.settings)
        }
        .sheet(item: Binding(
            get: { app.factory.pendingApproval },
            set: { if $0 == nil { app.factory.pendingApproval = nil } }
        )) { approval in
            ApprovalSheet(approval: approval) { approve, approveAll in
                app.factory.respondToApproval(approve: approve, approveAll: approveAll)
            }
        }
    }
}

struct ApprovalSheet: View {
    let approval: PendingApproval
    /// (approve, approveAll)
    let respond: (Bool, Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Image(systemName: approval.kind == .code ? "doc.badge.gearshape" : "cpu")
                    .font(.title2)
                    .foregroundStyle(approval.kind == .code ? Color.orange : Color.accentColor)
                Text(approval.kind == .code ? "Approve code change" : "Device write approval")
                    .font(.title2.weight(.semibold))
                Spacer()
            }
            Text(approval.explanation)
                .font(.callout)
                .foregroundStyle(.secondary)
            if approval.kind == .code {
                if !approval.files.isEmpty {
                    Text(approval.files.joined(separator: "\n"))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                DiffView(diff: approval.diff)
            } else {
                ScrollView {
                    Text(approval.detail)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(Color.primary.opacity(0.06))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
            controls
        }
        .padding(20)
        .presentationDetents(approval.kind == .code ? [.large] : [.medium])
    }

    private var controls: some View {
        HStack(spacing: 10) {
            Button("Reject", role: .destructive) { respond(false, false) }
            Spacer()
            if approval.kind == .code {
                Button("Approve all") { respond(true, true) }
                    .buttonStyle(.bordered)
            }
            Button("Approve") { respond(true, false) }
                .buttonStyle(.borderedProminent)
        }
    }
}

/// A minimal unified-diff renderer: green adds, red removes, dimmed hunk headers.
struct DiffView: View {
    let diff: String

    var body: some View {
        ScrollView([.vertical, .horizontal]) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                    Text(line.isEmpty ? " " : line)
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(color(for: line))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 1)
                        .background(background(for: line))
                }
            }
            .padding(.vertical, 6)
        }
        .background(Color.primary.opacity(0.04))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var lines: [String] { diff.components(separatedBy: "\n") }

    private func color(for line: String) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return .green }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return .red }
        if line.hasPrefix("@@") { return .secondary }
        return .primary
    }

    private func background(for line: String) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return Color.green.opacity(0.12) }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return Color.red.opacity(0.12) }
        return .clear
    }
}
