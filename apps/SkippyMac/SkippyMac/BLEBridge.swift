import CoreBluetooth
import Foundation

/// The BLE side of a bench node, folded into the app: SkippyMac as the relay
/// that `skippy_ble_bridge.py` is on a machine without the app.
///
/// The io-node firmware (`firmware/core2-devio`) advertises a Nordic-UART-style
/// service and speaks the same JSON lines over it that it would put on its own
/// hub WebSocket. This class is a CoreBluetooth central that finds the node,
/// proves itself with the factory token, then connects to the hub as
/// `devices:<node>` and relays lines both ways. The hub cannot tell this
/// transport from the node's own Wi-Fi socket — ADR 0021 pins the design.
///
/// One rule matters more than the plumbing: replies are forwarded as the node
/// sent them. Only `node_status` telemetry is touched, to stamp
/// `"transport": "ble"` so the app can say how the node is attached.
@MainActor
final class BLEBridge: NSObject, ObservableObject {
    enum Phase: Equatable {
        case off              // the button says Connect
        case unavailable(String)  // Bluetooth denied or powered off
        case scanning
        case connecting
        case authenticating
        case relaying
    }

    @Published private(set) var phase: Phase = .off
    @Published private(set) var nodeName: String = ""

    var active: Bool { phase != .off }

    var statusText: String {
        switch phase {
        case .off: return "Reaches a node with no Wi-Fi, over Bluetooth."
        case .unavailable(let why): return why
        case .scanning: return "Scanning for a skippy io-node…"
        case .connecting: return "Node found, connecting…"
        case .authenticating: return "Authenticating…"
        case .relaying: return "Relaying \(nodeName.isEmpty ? "node" : nodeName) to the hub."
        }
    }

    // Must match firmware/core2-devio/src/main.cpp and skippy_ble_bridge.py.
    private static let serviceUUID = CBUUID(string: "B7F80001-9A3C-4F4E-8A52-6E0D7C3B2A19")
    private static let rxUUID = CBUUID(string: "B7F80002-9A3C-4F4E-8A52-6E0D7C3B2A19")  // app -> node
    private static let txUUID = CBUUID(string: "B7F80003-9A3C-4F4E-8A52-6E0D7C3B2A19")  // node -> app

    private var settings: SettingsStore
    private var central: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var rxChar: CBCharacteristic?
    private var assembly = Data()
    /// Lines from the node that arrived before the hub socket opened — the
    /// hello and first node_status land in the gap while the WebSocket dials.
    private var pendingForHub: [String] = []
    private let hubSocket = WebSocketSession()
    private var hubConnected = false

    init(settings: SettingsStore) {
        self.settings = settings
        super.init()
        hubSocket.onMessage = { [weak self] msg in
            Task { @MainActor in self?.forwardToNode(msg) }
        }
        hubSocket.onStateChange = { [weak self] up in
            Task { @MainActor in self?.hubStateChanged(up) }
        }
    }

    // MARK: - The button

    func start() {
        guard phase == .off || {
            if case .unavailable = phase { return true } else { return false }
        }() else { return }
        phase = .scanning
        if central == nil {
            // Creating the manager is what triggers the one-time Bluetooth
            // permission prompt, so it waits for the first Connect.
            central = CBCentralManager(delegate: self, queue: .main)
        } else {
            scanIfPoweredOn()
        }
    }

    func stop() {
        phase = .off
        nodeName = ""
        central?.stopScan()
        if let peripheral {
            central?.cancelPeripheralConnection(peripheral)
        }
        teardownLink()
    }

    /// Everything but the manager and the user's intent.
    private func teardownLink() {
        peripheral = nil
        rxChar = nil
        assembly.removeAll()
        pendingForHub.removeAll()
        hubSocket.disconnect()
        hubConnected = false
    }

    private func scanIfPoweredOn() {
        guard let central, central.state == .poweredOn, active else { return }
        phase = .scanning
        central.scanForPeripherals(withServices: [Self.serviceUUID])
    }

    /// The node dropped or the link failed: keep the user's intent and try
    /// again from the scan, the same loop the Python bridge runs.
    private func restart() {
        guard active else { return }
        teardownLink()
        scanIfPoweredOn()
    }

    // MARK: - Node -> hub

    private func handleNodeLine(_ line: String) {
        var outbound = line
        if let data = line.data(using: .utf8),
           var obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            if obj["type"] as? String == "hello", let node = obj["node"] as? String, !node.isEmpty {
                // The node said who it is; that name is the client id the hub
                // routes by, so the hub socket can only open now.
                if nodeName != node {
                    nodeName = node
                    openHubSocket(node: node)
                }
                phase = .relaying
            }
            if obj["type"] as? String == "node_status" {
                obj["transport"] = "ble"
                if let stamped = try? JSONSerialization.data(withJSONObject: obj),
                   let text = String(data: stamped, encoding: .utf8) {
                    outbound = text
                }
            }
        }
        if hubConnected {
            hubSocket.sendText(outbound)
        } else {
            pendingForHub.append(outbound)
            if pendingForHub.count > 32 { pendingForHub.removeFirst() }
        }
    }

    private func openHubSocket(node: String) {
        hubSocket.connect(to: settings.benchBridgeURL(node: node))
    }

    private func hubStateChanged(_ up: Bool) {
        hubConnected = up
        guard up else { return }
        for line in pendingForHub { hubSocket.sendText(line) }
        pendingForHub.removeAll()
    }

    // MARK: - Hub -> node

    private func forwardToNode(_ msg: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(msg),
              let data = try? JSONSerialization.data(withJSONObject: msg),
              let text = String(data: data, encoding: .utf8) else { return }
        writeLine(text)
    }

    private func writeLine(_ line: String) {
        guard let peripheral, let rxChar else { return }
        var framed = Data(line.utf8)
        framed.append(0x0A)
        // With-response writes: CoreBluetooth queues them and the peripheral
        // acks each one, so a multi-chunk line cannot outrun the radio.
        let chunk = max(20, peripheral.maximumWriteValueLength(for: .withResponse))
        var offset = 0
        while offset < framed.count {
            let end = min(offset + chunk, framed.count)
            peripheral.writeValue(framed.subdata(in: offset..<end), for: rxChar, type: .withResponse)
            offset = end
        }
    }

    private func sendAuthHello() {
        phase = .authenticating
        let hello: [String: Any] = ["type": "hello", "token": settings.benchToken]
        if let data = try? JSONSerialization.data(withJSONObject: hello),
           let text = String(data: data, encoding: .utf8) {
            writeLine(text)
        }
    }
}

// MARK: - CoreBluetooth delegates
//
// The manager runs on the main queue, so these land on the actor they claim.

extension BLEBridge: CBCentralManagerDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        MainActor.assumeIsolated {
            switch central.state {
            case .poweredOn:
                scanIfPoweredOn()
            case .unauthorized:
                phase = .unavailable("Bluetooth access is denied — allow SkippyMac in System Settings > Privacy > Bluetooth.")
            case .poweredOff:
                phase = .unavailable("Bluetooth is off.")
            default:
                break
            }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                                    advertisementData: [String: Any], rssi RSSI: NSNumber) {
        MainActor.assumeIsolated {
            guard active, self.peripheral == nil else { return }
            central.stopScan()
            phase = .connecting
            self.peripheral = peripheral
            central.connect(peripheral)
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        MainActor.assumeIsolated {
            peripheral.delegate = self
            peripheral.discoverServices([Self.serviceUUID])
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral,
                                    error: Error?) {
        MainActor.assumeIsolated { restart() }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                                    error: Error?) {
        MainActor.assumeIsolated { restart() }
    }
}

extension BLEBridge: CBPeripheralDelegate {
    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        MainActor.assumeIsolated {
            guard let service = peripheral.services?.first(where: { $0.uuid == Self.serviceUUID }) else {
                restart()
                return
            }
            peripheral.discoverCharacteristics([Self.rxUUID, Self.txUUID], for: service)
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService,
                                error: Error?) {
        MainActor.assumeIsolated {
            for characteristic in service.characteristics ?? [] {
                if characteristic.uuid == Self.rxUUID { rxChar = characteristic }
                if characteristic.uuid == Self.txUUID {
                    peripheral.setNotifyValue(true, for: characteristic)
                }
            }
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral,
                                didUpdateNotificationStateFor characteristic: CBCharacteristic,
                                error: Error?) {
        MainActor.assumeIsolated {
            // Subscribed: the return path exists, so it is safe to speak.
            guard characteristic.uuid == Self.txUUID, error == nil, rxChar != nil else {
                if error != nil { restart() }
                return
            }
            sendAuthHello()
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic,
                                error: Error?) {
        MainActor.assumeIsolated {
            guard characteristic.uuid == Self.txUUID, let data = characteristic.value else { return }
            for byte in data {
                if byte == 0x0A {
                    if !assembly.isEmpty, let line = String(data: assembly, encoding: .utf8) {
                        handleNodeLine(line)
                    }
                    assembly.removeAll(keepingCapacity: true)
                } else {
                    assembly.append(byte)
                    // Not our protocol past this size; drop rather than hoard.
                    if assembly.count > 32_768 { assembly.removeAll(keepingCapacity: true) }
                }
            }
        }
    }
}
