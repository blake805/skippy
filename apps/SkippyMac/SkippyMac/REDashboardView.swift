import SwiftUI

/// The reverse-engineering cockpit: a device panel on the left, a live traffic
/// console and protocol scratchpad in the middle, and a findings notebook on
/// the right. Everything here is tailored to probing a part on the bench.
struct REDashboardView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        HSplitView {
            DevicePanel(re: app.re, factory: app.factory)
                .frame(minWidth: 240, idealWidth: 280, maxWidth: 360)
            VSplitView {
                TrafficConsole(re: app.re)
                    .frame(minHeight: 220)
                Scratchpad(re: app.re)
                    .frame(minHeight: 200)
            }
            .frame(minWidth: 360)
            FindingsNotebook(factory: app.factory)
                .frame(minWidth: 260, idealWidth: 320, maxWidth: 420)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear { app.openReverse() }
    }
}

// MARK: - Device panel

private struct DevicePanel: View {
    @ObservedObject var re: REStore
    @ObservedObject var factory: FactoryClient

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: "Devices", systemImage: "cpu") {
                re.refreshDevices()
                factory.requestStudioDevices()
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    deviceGroup("This Mac", re.localDevices, openable: true)
                    deviceGroup("Studio (hub)", factory.reStudioDevices, openable: false)
                    networkTarget
                }
                .padding(14)
            }
            if !re.error.isEmpty {
                Text(re.error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 10)
            }
        }
        .background(Theme.railBackground)
    }

    @ViewBuilder
    private func deviceGroup(_ title: String, _ devices: [REDevice], openable: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            if devices.isEmpty {
                Text("None detected")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            } else {
                ForEach(devices) { device in
                    DeviceRow(
                        device: device,
                        active: re.activeDevice?.id == device.id,
                        openable: openable
                    ) {
                        re.open(device)
                    }
                }
            }
        }
    }

    private var networkTarget: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NETWORK TARGET")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            HStack(spacing: 6) {
                TextField("host", text: $re.netAddress)
                    .textFieldStyle(.roundedBorder)
                TextField("port", text: $re.netPort)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 60)
            }
            Button {
                re.openNet()
            } label: {
                Label("Connect", systemImage: "network")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
        }
    }
}

private struct DeviceRow: View {
    let device: REDevice
    let active: Bool
    let openable: Bool
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .foregroundStyle(active ? Theme.accent(for: .re) : .secondary)
                    .frame(width: 18)
                VStack(alignment: .leading, spacing: 1) {
                    Text(device.label)
                        .font(.callout)
                        .lineLimit(1)
                    if !device.detail.isEmpty {
                        Text(device.detail)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
                Spacer()
                if active { StatusPill(text: "open", color: Theme.accent(for: .re)) }
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(active ? Theme.accent(for: .re).opacity(0.10) : Theme.cardBackground)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .disabled(!openable)
        .opacity(openable ? 1 : 0.6)
        .help(openable ? "Open a session" : "On the studio — drive from RE chat")
    }

    private var icon: String {
        switch device.kind {
        case .serial: return "cable.connector"
        case .usb: return "cable.connector.horizontal"
        case .net: return "network"
        }
    }
}

// MARK: - Traffic console

private struct TrafficConsole: View {
    @ObservedObject var re: REStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: "Traffic", systemImage: "dot.radiowaves.left.and.right") {
                re.clearConsole()
            } trailing: {
                if let device = re.activeDevice {
                    HStack(spacing: 8) {
                        StatusPill(text: device.label, color: Theme.accent(for: .re))
                        Button("Close") { re.close() }
                            .buttonStyle(.plain)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Divider()
            if re.frames.isEmpty {
                EmptyState(
                    icon: "dot.radiowaves.left.and.right",
                    title: "No traffic yet",
                    message: "Open a device and send bytes from the scratchpad below to see frames here."
                )
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 6) {
                            ForEach(re.frames) { frame in
                                FrameRow(frame: frame)
                                    .id(frame.id)
                            }
                        }
                        .padding(12)
                    }
                    .onChange(of: re.frames.count) { _, _ in
                        if let last = re.frames.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                }
            }
        }
    }
}

private struct FrameRow: View {
    let frame: TrafficFrame

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .font(.caption)
                .foregroundStyle(color)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(Self.timeFormatter.string(from: frame.timestamp))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.tertiary)
                    if !frame.bytes.isEmpty {
                        Text("\(frame.bytes.count) B")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                if frame.bytes.isEmpty {
                    Text(frame.note)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Text(HexDump.dump(frame.bytes))
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(color)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private var symbol: String {
        switch frame.direction {
        case .tx: return "arrow.up.circle.fill"
        case .rx: return "arrow.down.circle.fill"
        case .note: return "info.circle"
        }
    }

    private var color: Color {
        switch frame.direction {
        case .tx: return .blue
        case .rx: return .green
        case .note: return .secondary
        }
    }
}

// MARK: - Scratchpad

private struct Scratchpad: View {
    @ObservedObject var re: REStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader(title: "Scratchpad", systemImage: "square.and.pencil")
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Picker("", selection: $re.composeEncoding) {
                        ForEach(REStore.Encoding.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 160)
                    Spacer()
                    Stepper("read \(re.readBytes) B", value: $re.readBytes, in: 0...16384, step: 64)
                        .font(.caption)
                }
                TextEditor(text: $re.composeText)
                    .font(.system(.body, design: .monospaced))
                    .frame(height: 60)
                    .padding(6)
                    .background(Color(nsColor: .controlBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                HStack {
                    if let bytes = re.composedBytes() {
                        Text("\(bytes.count) byte(s)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("invalid hex")
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                    Spacer()
                    Button {
                        re.send()
                    } label: {
                        Label("Send", systemImage: "paperplane.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.accent(for: .re))
                    .disabled(!re.isOpen || re.busy)
                }
                if !re.previousResponse.isEmpty {
                    ResponseDiff(
                        previous: re.previousResponse,
                        latest: re.lastResponse,
                        changed: re.diffOffsets
                    )
                }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 12)
        }
    }
}

/// Highlights the bytes that changed between the last two responses — the fast
/// path to "which field is the counter".
private struct ResponseDiff: View {
    let previous: [UInt8]
    let latest: [UInt8]
    let changed: Set<Int>

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("RESPONSE DIFF — \(changed.count) byte(s) changed")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            FlowHex(bytes: latest, highlighted: changed)
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

/// A wrapping row of hex bytes with changed offsets tinted.
private struct FlowHex: View {
    let bytes: [UInt8]
    let highlighted: Set<Int>

    var body: some View {
        Text(attributed)
            .font(.system(.caption, design: .monospaced))
            .textSelection(.enabled)
    }

    private var attributed: AttributedString {
        var out = AttributedString()
        for (i, byte) in bytes.enumerated() {
            var piece = AttributedString(String(format: "%02x ", byte))
            if highlighted.contains(i) {
                piece.foregroundColor = .orange
                piece.inlinePresentationIntent = .stronglyEmphasized
            } else {
                piece.foregroundColor = .secondary
            }
            out.append(piece)
        }
        return out
    }
}

// MARK: - Findings notebook

private struct FindingsNotebook: View {
    @ObservedObject var factory: FactoryClient
    @State private var composing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(title: "Findings", systemImage: "book.closed") {
                factory.requestPacks()
                if !factory.reOpenPackId.isEmpty {
                    factory.requestPack(factory.reOpenPackId)
                }
            } trailing: {
                Button {
                    composing = true
                } label: {
                    Image(systemName: "plus")
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.accent(for: .re))
            }
            Divider()
            content
        }
        .background(Theme.railBackground)
        .sheet(isPresented: $composing) {
            FindingComposer(
                factory: factory,
                defaultPackTarget: factory.rePacks.first(where: {
                    $0.id == factory.reOpenPackId
                })?.target ?? ""
            )
        }
    }

    @ViewBuilder
    private var content: some View {
        if factory.rePacks.isEmpty {
            EmptyState(
                icon: "book.closed",
                title: "No note packs yet",
                message: "RE findings are recorded as packs, one per target. Add one with the + above, or let Skippy note them during a run."
            )
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Picker("Pack", selection: Binding(
                        get: { factory.reOpenPackId },
                        set: { factory.requestPack($0) }
                    )) {
                        Text("Select a pack").tag("")
                        ForEach(factory.rePacks) { pack in
                            Text("\(pack.title.isEmpty ? pack.id : pack.title) (\(pack.findings))")
                                .tag(pack.id)
                        }
                    }
                    .labelsHidden()
                    if factory.reOpenPackId.isEmpty {
                        Text("Pick a pack to read its findings.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    } else if factory.reFindings.isEmpty {
                        Text("No findings in this pack yet.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    } else {
                        ForEach(factory.reFindings) { finding in
                            FindingRow(finding: finding)
                        }
                    }
                }
                .padding(14)
            }
            if !factory.reNotice.isEmpty {
                Text(factory.reNotice)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 10)
            }
        }
    }
}

private struct FindingRow: View {
    let finding: REFinding
    @State private var expanded = false

    private var confidenceColor: Color {
        switch finding.confidence {
        case "confirmed": return .green
        case "likely": return .yellow
        default: return .secondary
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(finding.id)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                StatusPill(text: finding.kind, color: Theme.accent(for: .re))
                StatusPill(text: finding.confidence, color: confidenceColor)
                if finding.superseded { StatusPill(text: "superseded", color: .secondary) }
            }
            Text(finding.title)
                .font(.callout.weight(.medium))
                .strikethrough(finding.superseded, color: .secondary)
            if !finding.location.isEmpty {
                Text(finding.location)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            DisclosureGroup(isExpanded: $expanded) {
                Text(finding.text)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } label: {
                Text(expanded ? "Hide" : "Detail")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

/// Records a human-authored finding. Same evidence discipline as Skippy's own:
/// the hub refuses an assertion with no evidence.
private struct FindingComposer: View {
    @ObservedObject var factory: FactoryClient
    @Environment(\.dismiss) private var dismiss
    let defaultPackTarget: String

    @State private var target = ""
    @State private var kind = "structure"
    @State private var title = ""
    @State private var body_ = ""
    @State private var evidence = ""
    @State private var confidence = "likely"
    @State private var location = ""

    private let kinds = ["structure", "behavior", "constant", "symbol", "hypothesis", "question"]
    private let confidences = ["speculative", "likely", "confirmed"]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New finding")
                .font(.title2.weight(.semibold))
            HStack {
                TextField("Target (e.g. acme-fob.bin)", text: $target)
                    .textFieldStyle(.roundedBorder)
            }
            HStack {
                Picker("Kind", selection: $kind) {
                    ForEach(kinds, id: \.self) { Text($0).tag($0) }
                }
                Picker("Confidence", selection: $confidence) {
                    ForEach(confidences, id: \.self) { Text($0).tag($0) }
                }
            }
            TextField("Title", text: $title)
                .textFieldStyle(.roundedBorder)
            TextField("Location (offset / symbol, optional)", text: $location)
                .textFieldStyle(.roundedBorder)
            VStack(alignment: .leading, spacing: 4) {
                Text("What you found").font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $body_).frame(height: 60).border(Color.secondary.opacity(0.2))
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Evidence (where you saw it) — required unless kind is 'question'")
                    .font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $evidence).frame(height: 50).border(Color.secondary.opacity(0.2))
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Save") {
                    factory.addFinding([
                        "target": target.isEmpty ? defaultPackTarget : target,
                        "kind": kind,
                        "title": title,
                        "body": body_,
                        "evidence": evidence,
                        "confidence": confidence,
                        "location": location,
                    ])
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent(for: .re))
                .disabled(title.isEmpty)
            }
        }
        .padding(24)
        .frame(width: 480)
        .onAppear { if target.isEmpty { target = defaultPackTarget } }
    }
}

// MARK: - Shared

private struct SectionHeader<Trailing: View>: View {
    let title: String
    let systemImage: String
    var onRefresh: (() -> Void)?
    @ViewBuilder var trailing: () -> Trailing

    init(
        title: String,
        systemImage: String,
        onRefresh: (() -> Void)? = nil,
        @ViewBuilder trailing: @escaping () -> Trailing = { EmptyView() }
    ) {
        self.title = title
        self.systemImage = systemImage
        self.onRefresh = onRefresh
        self.trailing = trailing
    }

    var body: some View {
        HStack(spacing: 8) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            Spacer()
            trailing()
            if let onRefresh {
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}
