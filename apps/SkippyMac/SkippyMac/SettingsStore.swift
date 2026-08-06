import Foundation
import Combine

/// Persisted preferences. The hub lives on the Mac Studio; this Mac is the seat.
final class SettingsStore: ObservableObject {
    @Published var hubHost: String {
        didSet { UserDefaults.standard.set(hubHost, forKey: Keys.hubHost) }
    }
    @Published var hubPort: Int {
        didSet { UserDefaults.standard.set(hubPort, forKey: Keys.hubPort) }
    }
    @Published var voiceToken: String {
        didSet { UserDefaults.standard.set(voiceToken, forKey: Keys.voiceToken) }
    }
    @Published var shareDevices: Bool {
        didSet { UserDefaults.standard.set(shareDevices, forKey: Keys.shareDevices) }
    }
    @Published var clientId: String {
        didSet { UserDefaults.standard.set(clientId, forKey: Keys.clientId) }
    }
    @Published var benchToken: String {
        didSet { UserDefaults.standard.set(benchToken, forKey: Keys.benchToken) }
    }

    var factoryURL: URL {
        URL(string: "ws://\(hubHost):\(hubPort)/ws/factory?client_id=\(clientId)")!
    }

    var devicesBridgeURL: URL {
        URL(string: "ws://\(hubHost):\(hubPort)/ws/factory?client_id=devices")!
    }

    /// Where the BLE bridge presents a bench node: the node's own client id,
    /// so the hub cannot tell this relay from the node's Wi-Fi socket.
    func benchBridgeURL(node: String) -> URL {
        var s = "ws://\(hubHost):\(hubPort)/ws/factory?client_id=devices:\(node)"
        if !benchToken.isEmpty {
            s += "&token=\(benchToken)"
        }
        return URL(string: s)!
    }

    var voiceURL: URL {
        var s = "ws://\(hubHost):\(hubPort)/ws/voice"
        if !voiceToken.isEmpty {
            s += "?token=\(voiceToken)"
        }
        return URL(string: s)!
    }

    init() {
        let defaults = UserDefaults.standard
        hubHost = defaults.string(forKey: Keys.hubHost) ?? "192.168.1.151"
        let port = defaults.integer(forKey: Keys.hubPort)
        hubPort = port == 0 ? 8000 : port
        voiceToken = defaults.string(forKey: Keys.voiceToken) ?? ""
        shareDevices = defaults.object(forKey: Keys.shareDevices) as? Bool ?? true
        clientId = defaults.string(forKey: Keys.clientId) ?? "skippy-mac"
        benchToken = defaults.string(forKey: Keys.benchToken) ?? ""
    }

    private enum Keys {
        static let hubHost = "skippy.hubHost"
        static let hubPort = "skippy.hubPort"
        static let voiceToken = "skippy.voiceToken"
        static let shareDevices = "skippy.shareDevices"
        static let clientId = "skippy.clientId"
        static let benchToken = "skippy.benchToken"
    }
}
