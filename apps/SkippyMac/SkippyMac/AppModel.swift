import Foundation
import Combine

@MainActor
final class AppModel: ObservableObject {
    @Published var page: SidebarPage = .work
    @Published var mode: AgentMode = .coding
    @Published var draft: String = ""
    @Published var reTarget: String = ""

    let settings: SettingsStore
    let factory: FactoryClient
    let voice: VoiceClient
    let devices: DeviceBridge
    let re: REStore

    private var cancellables = Set<AnyCancellable>()

    var isRunning: Bool { factory.isRunning }

    init() {
        let settings = SettingsStore()
        self.settings = settings
        self.factory = FactoryClient(settings: settings)
        self.voice = VoiceClient(settings: settings)
        let devices = DeviceBridge(settings: settings)
        self.devices = devices
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
        factory.send(text: draft, mode: mode, target: reTarget)
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
        if !factory.gitSelectedRepo.isEmpty {
            factory.requestGitDetail(factory.gitSelectedRepo)
        }
    }

    /// Pull everything the RE dashboard needs when it opens: this Mac's devices,
    /// the studio's, and the note packs.
    func openReverse() {
        page = .reverse
        re.refreshDevices()
        factory.requestStudioDevices()
        factory.requestPacks()
    }
}
