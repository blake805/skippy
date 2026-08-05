import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        NavigationSplitView {
            List(SidebarPage.allCases, selection: Binding(
                get: { app.page },
                set: { newPage in
                    app.page = newPage
                    if newPage == .voice { app.openVoice() }
                    if newPage == .reverse { app.openReverse() }
                    if newPage == .repo { app.openRepo() }
                }
            )) { page in
                Label(page.rawValue, systemImage: page.systemImage)
                    .tag(page)
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 160, ideal: 180, max: 220)
            .safeAreaInset(edge: .bottom) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(app.factory.connected ? Color.green : Color.orange)
                            .frame(width: 8, height: 8)
                        Text(app.factory.statusLine)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Text(app.settings.hubHost)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } detail: {
            switch app.page {
            case .work:
                ChatView()
            case .reverse:
                REDashboardView()
            case .repo:
                RepoView(factory: app.factory)
            case .voice:
                VoiceView(voice: app.voice)
            case .settings:
                SettingsView()
            }
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
            header
            if approval.kind == .code {
                if !approval.files.isEmpty {
                    Text(approval.files.joined(separator: "   "))
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                DiffView(diff: approval.diff)
                    .frame(minHeight: 220, maxHeight: 420)
            } else {
                Text(approval.explanation).font(.body)
                ScrollView {
                    Text(approval.detail)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(Color.primary.opacity(0.06))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .frame(maxHeight: 220)
            }
            controls
        }
        .padding(24)
        .frame(width: approval.kind == .code ? 640 : 520)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: approval.kind == .code ? "doc.badge.gearshape" : "cpu")
                .font(.title2)
                .foregroundStyle(approval.kind == .code ? Color.orange : Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text(approval.kind == .code ? "Approve code change" : "Device write approval")
                    .font(.title2.weight(.semibold))
                if approval.kind == .code {
                    Text(approval.explanation)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Spacer()
            Button("Reject", role: .destructive) { respond(false, false) }
                .keyboardShortcut(.cancelAction)
            if approval.kind == .code {
                Button("Approve all") { respond(true, true) }
                    .buttonStyle(.bordered)
            }
            Button("Approve") { respond(true, false) }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
        }
    }
}

/// A minimal unified-diff renderer: green adds, red removes, dimmed hunk
/// headers. Deliberately not a full syntax highlighter — the job here is to let
/// someone glance at a change and decide, not to reproduce the editor.
struct DiffView: View {
    let diff: String

    var body: some View {
        ScrollView([.vertical, .horizontal]) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                    Text(line.isEmpty ? " " : line)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(color(for: line))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 10)
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
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return Color.green.opacity(0.10) }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return Color.red.opacity(0.10) }
        return .clear
    }
}
