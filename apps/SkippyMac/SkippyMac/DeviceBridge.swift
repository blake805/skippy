import Darwin
import Foundation
import Network

/// Answers `device_*` RPCs from the hub for hardware attached to this Mac.
///
/// Serial uses Foundation FileHandle against `/dev/cu.*`. USB raw transfers need
/// IOKit/libusb wiring that lands in a follow-up; until then USB actions return a
/// clear "not yet" error rather than pretending. Network uses Network.framework.
@MainActor
final class DeviceBridge: ObservableObject {
    @Published var enabled = true
    @Published var lastError: String = ""

    private var serialHandles: [String: FileHandle] = [:]
    private var netConnections: [String: NWConnection] = [:]
    private let bridgeSocket = WebSocketSession()
    private var settings: SettingsStore

    init(settings: SettingsStore) {
        self.settings = settings
        bridgeSocket.onMessage = { [weak self] msg in
            Task { @MainActor in
                await self?.handleIncoming(msg)
            }
        }
    }

    func start() {
        guard enabled else { return }
        bridgeSocket.connect(to: settings.devicesBridgeURL)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.bridgeSocket.sendJSON(["type": "hello", "role": "devices"])
        }
    }

    func stop() {
        bridgeSocket.disconnect()
        for (_, fh) in serialHandles { try? fh.close() }
        serialHandles.removeAll()
        for (_, conn) in netConnections { conn.cancel() }
        netConnections.removeAll()
    }

    func updateSettings(_ settings: SettingsStore) {
        self.settings = settings
        self.enabled = settings.shareDevices
    }

    // MARK: - Interactive local session (RE dashboard)
    //
    // The dashboard drives this Mac's own hardware directly, without an agent
    // run in the loop — the same backends the hub calls over RPC, reached in
    // process. Studio hardware still goes through the hub.

    struct DeviceResult {
        let ok: Bool
        let handle: String
        let bytes: [UInt8]
        let error: String
    }

    func enumerateLocal() async -> [REDevice] {
        let reply = await handle(["action": "device_list", "kinds": ["serial", "usb"]])
        guard let result = reply["result"] as? [String: Any],
              let devices = result["devices"] as? [[String: Any]] else { return [] }
        return devices.map { REDevice(from: $0) }
    }

    func openSerialLocal(port: String) async -> DeviceResult {
        let reply = await handle(["action": "device_serial_open", "port": port])
        return Self.result(from: reply)
    }

    func openNetLocal(address: String, port: Int) async -> DeviceResult {
        let reply = await handle(["action": "device_net_connect", "address": address, "port": port])
        return Self.result(from: reply)
    }

    func serialExchange(handle sessionHandle: String, writeHex: String, readBytes: Int, captureSeconds: Double) async -> DeviceResult {
        var msg: [String: Any] = ["action": "device_serial_io", "handle": sessionHandle]
        if !writeHex.isEmpty { msg["write_hex"] = writeHex }
        if readBytes > 0 { msg["read_bytes"] = readBytes }
        if captureSeconds > 0 { msg["capture_seconds"] = captureSeconds }
        return Self.result(from: await handle(msg))
    }

    func netExchange(handle sessionHandle: String, writeHex: String, readBytes: Int) async -> DeviceResult {
        var msg: [String: Any] = ["action": "device_net_io", "handle": sessionHandle]
        if !writeHex.isEmpty { msg["write_hex"] = writeHex }
        if readBytes > 0 { msg["read_bytes"] = readBytes }
        return Self.result(from: await handle(msg))
    }

    func closeLocal(handle sessionHandle: String, kind: REDevice.Kind) async {
        let action = kind == .net ? "device_net_close" : "device_serial_close"
        _ = await handle(["action": action, "handle": sessionHandle])
    }

    private static func result(from reply: [String: Any]) -> DeviceResult {
        if reply["ok"] as? Bool == true {
            let result = reply["result"] as? [String: Any] ?? [:]
            let handle = result["handle"] as? String ?? ""
            let bytes = (result["data_hex"] as? String).flatMap { HexDump.parse($0) } ?? []
            return DeviceResult(ok: true, handle: handle, bytes: bytes, error: "")
        }
        return DeviceResult(ok: false, handle: "", bytes: [], error: reply["error"] as? String ?? "device error")
    }

    /// Used when the main UI socket receives a device RPC (single-connection mode).
    func handle(_ msg: [String: Any]) async -> [String: Any] {
        let action = msg["action"] as? String ?? ""
        do {
            switch action {
            case "device_list":
                return try listDevices(kinds: msg["kinds"] as? [String] ?? ["serial", "usb"])
            case "device_serial_open":
                return try serialOpen(msg)
            case "device_serial_io":
                return try serialIO(msg)
            case "device_serial_close":
                return serialClose(msg)
            case "device_net_connect":
                return try await netConnect(msg)
            case "device_net_io":
                return try await netIO(msg)
            case "device_net_close":
                return netClose(msg)
            case "device_net_scan":
                return try await netScan(msg)
            case "device_usb_transfer", "device_usb_control":
                return ["ok": false, "error": "USB bridge on macOS is not wired yet; plug the device into the Mac Studio (host=studio) for now."]
            default:
                return ["ok": false, "error": "Unknown device action \(action)"]
            }
        } catch {
            lastError = error.localizedDescription
            return ["ok": false, "error": error.localizedDescription]
        }
    }

    private func handleIncoming(_ msg: [String: Any]) async {
        guard let action = msg["action"] as? String, action.hasPrefix("device_"),
              let taskId = msg["task_id"] as? String else { return }
        var reply = await handle(msg)
        reply["task_id"] = taskId
        bridgeSocket.sendJSON(reply)
    }

    // MARK: - Serial

    private func listDevices(kinds: [String]) throws -> [String: Any] {
        var devices: [[String: Any]] = []
        if kinds.contains("serial") {
            let fm = FileManager.default
            let dev = "/dev"
            let names = (try? fm.contentsOfDirectory(atPath: dev)) ?? []
            for name in names where name.hasPrefix("cu.") {
                devices.append([
                    "kind": "serial",
                    "host": "macbook",
                    "port": "\(dev)/\(name)",
                    "description": name,
                    "manufacturer": "",
                    "vid": "",
                    "pid": "",
                    "serial_number": "",
                ])
            }
        }
        // USB enumeration via IOKit is a follow-up; return empty for usb on bridge.
        return ["ok": true, "result": ["devices": devices]]
    }

    private func serialOpen(_ msg: [String: Any]) throws -> [String: Any] {
        guard let port = msg["port"] as? String else {
            return ["ok": false, "error": "port required"]
        }
        let handle = (msg["handle"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            ?? "ser_\(UUID().uuidString.prefix(10))"
        let fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK)
        guard fd >= 0 else {
            return ["ok": false, "error": "Could not open \(port): \(String(cString: strerror(errno)))"]
        }
        let fh = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        serialHandles[handle] = fh
        return ["ok": true, "result": ["handle": handle, "port": port]]
    }

    private func serialIO(_ msg: [String: Any]) throws -> [String: Any] {
        guard let handle = msg["handle"] as? String, let fh = serialHandles[handle] else {
            return ["ok": false, "error": "Unknown serial handle"]
        }
        if let hex = msg["write_hex"] as? String, !hex.isEmpty {
            let data = try Data(hexString: hex)
            try fh.write(contentsOf: data)
        }
        let readBytes = msg["read_bytes"] as? Int ?? 0
        let capture = msg["capture_seconds"] as? Double ?? 0
        let idle = msg["read_until_idle"] as? Double ?? 0
        var collected = Data()
        let deadline: Date
        if capture > 0 {
            deadline = Date().addingTimeInterval(min(capture, 30))
        } else if idle > 0 {
            deadline = Date().addingTimeInterval(min(idle, 30))
        } else {
            deadline = Date().addingTimeInterval(2)
        }
        while Date() < deadline, collected.count < 16_384 {
            if let chunk = try? fh.read(upToCount: min(1024, max(1, readBytes > 0 ? readBytes - collected.count : 1024))),
               !chunk.isEmpty {
                collected.append(chunk)
                if readBytes > 0, collected.count >= readBytes { break }
            } else {
                if capture <= 0, !collected.isEmpty { break }
                usleep(50_000)
            }
        }
        return ["ok": true, "result": ["data_hex": collected.hexString]]
    }

    private func serialClose(_ msg: [String: Any]) -> [String: Any] {
        guard let handle = msg["handle"] as? String else {
            return ["ok": false, "error": "handle required"]
        }
        if let fh = serialHandles.removeValue(forKey: handle) {
            try? fh.close()
        }
        return ["ok": true, "result": ["handle": handle]]
    }

    // MARK: - Network

    private func netConnect(_ msg: [String: Any]) async throws -> [String: Any] {
        guard let address = msg["address"] as? String,
              let port = msg["port"] as? Int else {
            return ["ok": false, "error": "address and port required"]
        }
        let proto = (msg["proto"] as? String ?? "tcp").lowercased()
        let handle = (msg["handle"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            ?? "net_\(UUID().uuidString.prefix(10))"
        let nwPort = NWEndpoint.Port(rawValue: UInt16(port))!
        let params: NWParameters = proto == "udp" ? .udp : .tcp
        let conn = NWConnection(host: NWEndpoint.Host(address), port: nwPort, using: params)
        let ok = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            conn.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    cont.resume(returning: true)
                case .failed, .cancelled:
                    cont.resume(returning: false)
                default:
                    break
                }
            }
            conn.start(queue: .global())
        }
        guard ok else { return ["ok": false, "error": "connect failed"] }
        netConnections[handle] = conn
        return ["ok": true, "result": ["handle": handle]]
    }

    private func netIO(_ msg: [String: Any]) async throws -> [String: Any] {
        guard let handle = msg["handle"] as? String, let conn = netConnections[handle] else {
            return ["ok": false, "error": "Unknown net handle"]
        }
        if let hex = msg["write_hex"] as? String, !hex.isEmpty {
            let data = try Data(hexString: hex)
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                conn.send(content: data, completion: .contentProcessed { error in
                    if let error { cont.resume(throwing: error) } else { cont.resume() }
                })
            }
        }
        let readBytes = msg["read_bytes"] as? Int ?? 0
        guard readBytes > 0 else {
            return ["ok": true, "result": ["data_hex": ""]]
        }
        let data: Data = try await withCheckedThrowingContinuation { cont in
            conn.receive(minimumIncompleteLength: 1, maximumLength: min(readBytes, 16_384)) { content, _, _, error in
                if let error { cont.resume(throwing: error) }
                else { cont.resume(returning: content ?? Data()) }
            }
        }
        return ["ok": true, "result": ["data_hex": data.hexString]]
    }

    private func netClose(_ msg: [String: Any]) -> [String: Any] {
        guard let handle = msg["handle"] as? String else {
            return ["ok": false, "error": "handle required"]
        }
        if let conn = netConnections.removeValue(forKey: handle) {
            conn.cancel()
        }
        return ["ok": true, "result": ["handle": handle]]
    }

    private func netScan(_ msg: [String: Any]) async throws -> [String: Any] {
        guard let address = msg["address"] as? String else {
            return ["ok": false, "error": "address required"]
        }
        let ports = msg["ports"] as? [Int] ?? []
        let timeout = msg["timeout"] as? Double ?? 0.4
        var open: [Int] = []
        for port in ports.prefix(256) {
            if await tcpOpen(host: address, port: port, timeout: timeout) {
                open.append(port)
            }
        }
        return ["ok": true, "result": ["open": open]]
    }

    private func tcpOpen(host: String, port: Int, timeout: Double) async -> Bool {
        guard let nwPort = NWEndpoint.Port(rawValue: UInt16(port)) else { return false }
        let conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: .tcp)
        return await withCheckedContinuation { cont in
            var resumed = false
            let finish: (Bool) -> Void = { ok in
                guard !resumed else { return }
                resumed = true
                conn.cancel()
                cont.resume(returning: ok)
            }
            conn.stateUpdateHandler = { state in
                switch state {
                case .ready: finish(true)
                case .failed, .cancelled: finish(false)
                default: break
                }
            }
            conn.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                finish(false)
            }
        }
    }
}

private extension Data {
    var hexString: String { map { String(format: "%02x", $0) }.joined() }

    init(hexString: String) throws {
        let cleaned = hexString.replacingOccurrences(of: "\\s", with: "", options: .regularExpression)
        guard cleaned.count % 2 == 0 else { throw NSError(domain: "hex", code: 1) }
        var data = Data(capacity: cleaned.count / 2)
        var idx = cleaned.startIndex
        while idx < cleaned.endIndex {
            let next = cleaned.index(idx, offsetBy: 2)
            guard let byte = UInt8(cleaned[idx..<next], radix: 16) else {
                throw NSError(domain: "hex", code: 2)
            }
            data.append(byte)
            idx = next
        }
        self = data
    }
}
