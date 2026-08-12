import Foundation
import Combine

@MainActor
final class AppModel: ObservableObject {
    @Published var page: SidebarPage = .work
    @Published var mode: AgentMode = .chat
    @Published var draft: String = ""
    @Published var reTarget: String = ""

    let settings: SettingsStore
    let factory: FactoryClient
    let voice: VoiceClient

    private var cancellables = Set<AnyCancellable>()

    var isRunning: Bool { factory.isRunning }

    init() {
        let settings = SettingsStore()
        self.settings = settings
        self.factory = FactoryClient(settings: settings)
        self.voice = VoiceClient(settings: settings)

        // No device bridge on the phone: there is nothing plugged into it that
        // RE mode could use. The hub simply never sees a "devices" client here.
        factory.onDeviceRPC = { _ in
            ["ok": false, "error": "This iPhone does not bridge hardware devices."]
        }

        settings.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        // SwiftUI does not observe nested ObservableObjects. ChatView and
        // MemoryView read factory state through this model, so factory's
        // changes must be republished.
        factory.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }.store(in: &cancellables)

        connectAll()
    }

    func connectAll() {
        factory.updateSettings(settings)
        voice.updateSettings(settings)
        factory.connect()
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

    /// Reopen a past chat from the Memory tab and jump to Work so the
    /// transcript is visible.
    func openPastChat(_ chatId: String) {
        mode = .chat
        page = .work
        factory.openChat(chatId)
    }
}
