import Foundation
import Combine

@MainActor
final class AppModel: ObservableObject {
    @Published var page: SidebarPage = .work
    @Published var mode: AgentMode = .coding
    @Published var draft: String = ""
    @Published var reTarget: String = ""
    /// Which host RE device I/O should go through: "" means local/agent's
    /// choice, otherwise a bench node's host name (e.g. "bench"). The hub's
    /// run payload has no host field — the agent reads it from the task text —
    /// so send() writes the selection into the text.
    @Published var reHost: String = ""

    let settings: SettingsStore
    let factory: FactoryClient
    let voice: VoiceClient
    let devices: DeviceBridge
    let ble: BLEBridge
    let re: REStore

    private var cancellables = Set<AnyCancellable>()
    /// Re-asks for bridge nodes while the RE dashboard is on screen. 15s is
    /// the node's own telemetry period — polling faster only re-reads the
    /// same status.
    private var benchRefreshTimer: Timer?

    var isRunning: Bool { factory.isRunning }

    init() {
        let settings = SettingsStore()
        self.settings = settings
        self.factory = FactoryClient(settings: settings)
        self.voice = VoiceClient(settings: settings)
        let devices = DeviceBridge(settings: settings)
        self.devices = devices
        self.ble = BLEBridge(settings: settings)
        self.re = REStore(bridge: devices)

        // Main UI socket can also answer device RPCs when shareDevices is on,
        // so a single connection is enough; the dedicated devices socket covers
        // the case where the UI client_id differs from "devices".
        factory.onDeviceRPC = { [weak self] msg in
            guard let self, self.settings.shareDevices else {
                return ["ok": false, "error": "Device sharing is disabled in Settings."]
            }
            return await self.devices.handle(msg)
        }

        settings.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        // SwiftUI does not observe nested ObservableObjects. ContentView and
        // ChatView read factory state through this model (the approval sheet,
        // run cards, status line), so factory's changes must be republished —
        // without this they only redraw when something else touches the view
        // tree, which is a stuck approval sheet and a timeline that lags until
        // the next click.
        factory.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        ble.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        connectAll()
    }

    func connectAll() {
        factory.updateSettings(settings)
        voice.updateSettings(settings)
        devices.updateSettings(settings)
        factory.connect()
        if settings.shareDevices {
            devices.start()
        } else {
            devices.stop()
        }
    }

    func send() {
        var text = draft
        if mode == .re, !reHost.isEmpty {
            text += "\n\nUse host=\"\(reHost)\" for device I/O — it is the selected bench node."
        }
        factory.send(text: text, mode: mode, target: reTarget)
        draft = ""
    }

    func cancelRun() {
        factory.cancel()
    }

    func openVoice() {
        page = .voice
        voice.connect()
        voice.ensureListening()
    }

    /// Refresh the repo panel's data when it opens.
    func openRepo() {
        page = .repo
        factory.requestGitRepos()
        factory.requestGitHubStatus()
        if !factory.gitSelectedRepo.isEmpty {
            factory.requestGitDetail(factory.gitSelectedRepo)
        }
    }

    /// Pull everything the RE dashboard needs when it opens: this Mac's devices,
    /// the studio's, the bench nodes, and the note packs.
    func openReverse() {
        page = .reverse
        re.refreshDevices()
        factory.requestStudioDevices()
        factory.requestBridgeNodes()
        factory.requestPacks()
        benchRefreshTimer?.invalidate()
        benchRefreshTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.factory.requestBridgeNodes() }
        }
    }

    /// The RE dashboard went away; stop asking after bench nodes.
    func closeReverse() {
        benchRefreshTimer?.invalidate()
        benchRefreshTimer = nil
    }

    /// A bench node cannot be opened from the panel — remote I/O runs through
    /// an agent. Instead, select it as the RE chat's host and go there.
    func useBenchNodeInChat(_ host: String) {
        mode = .re
        reHost = host
        page = .work
    }

    /// The Bluetooth bridge button: connect a nearby io-node to the hub over
    /// BLE, or take it back off the air.
    func toggleBleBridge() {
        if ble.active {
            ble.stop()
            // The hub marks the node offline when the relay drops; show that
            // now rather than at the next 15s poll.
            factory.requestBridgeNodes()
        } else {
            ble.start()
        }
    }
}
