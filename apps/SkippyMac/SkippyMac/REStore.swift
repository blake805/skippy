import Foundation
import Combine

/// Drives an interactive reverse-engineering session against this Mac's own
/// hardware: enumerate, open a serial or TCP session, send composed bytes, and
/// log every frame to the traffic console. Studio hardware is listed for
/// reference but driven through RE chat, since that needs an agent run.
@MainActor
final class REStore: ObservableObject {
    @Published var localDevices: [REDevice] = []
    @Published var activeDevice: REDevice?
    @Published var frames: [TrafficFrame] = []
    @Published var busy = false
    @Published var error = ""

    // Composer state.
    @Published var composeText = ""
    @Published var composeEncoding: Encoding = .hex
    @Published var readBytes = 256
    @Published var captureSeconds = 0.0

    // Network target entry.
    @Published var netAddress = ""
    @Published var netPort = "80"

    // The last two responses, for the diff view.
    @Published var lastResponse: [UInt8] = []
    @Published var previousResponse: [UInt8] = []

    enum Encoding: String, CaseIterable, Identifiable {
        case hex = "Hex"
        case text = "Text"
        var id: String { rawValue }
    }

    private let bridge: DeviceBridge
    private var handle = ""

    init(bridge: DeviceBridge) {
        self.bridge = bridge
    }

    var isOpen: Bool { activeDevice != nil && !handle.isEmpty }

    var diffOffsets: Set<Int> {
        guard !previousResponse.isEmpty else { return [] }
        return HexDump.diffOffsets(previousResponse, lastResponse)
    }

    func refreshDevices() {
        Task {
            busy = true
            localDevices = await bridge.enumerateLocal()
            busy = false
        }
    }

    func open(_ device: REDevice) {
        guard device.isLocal else {
            error = "\(device.label) is on the studio — drive it from RE chat for now."
            return
        }
        Task {
            await closeActive()
            busy = true
            let result = await bridge.openSerialLocal(port: device.port)
            busy = false
            if result.ok {
                handle = result.handle
                activeDevice = device
                error = ""
                append(.note, note: "Opened \(device.label)")
            } else {
                error = result.error
            }
        }
    }

    func openNet() {
        let address = netAddress.trimmingCharacters(in: .whitespaces)
        guard !address.isEmpty, let port = Int(netPort), (1...65535).contains(port) else {
            error = "Enter a host and a port between 1 and 65535."
            return
        }
        Task {
            await closeActive()
            busy = true
            let result = await bridge.openNetLocal(address: address, port: port)
            busy = false
            if result.ok {
                handle = result.handle
                activeDevice = REDevice(netAddress: address, port: port)
                error = ""
                append(.note, note: "Connected \(address):\(port)")
            } else {
                error = result.error
            }
        }
    }

    /// Turn the composer into bytes, respecting the selected encoding.
    func composedBytes() -> [UInt8]? {
        switch composeEncoding {
        case .hex:
            return HexDump.parse(composeText)
        case .text:
            return Array(composeText.utf8)
        }
    }

    func send() {
        guard let device = activeDevice, !handle.isEmpty else {
            error = "Open a device first."
            return
        }
        guard let bytes = composedBytes() else {
            error = "That is not valid hex. Switch to Text, or fix the digits."
            return
        }
        let writeHex = HexDump.hex(bytes).replacingOccurrences(of: " ", with: "")
        Task {
            busy = true
            error = ""
            if !bytes.isEmpty { append(.tx, bytes: bytes) }
            let result: DeviceBridge.DeviceResult
            if device.kind == .net {
                result = await bridge.netExchange(handle: handle, writeHex: writeHex, readBytes: readBytes)
            } else {
                result = await bridge.serialExchange(
                    handle: handle, writeHex: writeHex,
                    readBytes: captureSeconds > 0 ? 0 : readBytes,
                    captureSeconds: captureSeconds
                )
            }
            busy = false
            if result.ok {
                if !result.bytes.isEmpty {
                    append(.rx, bytes: result.bytes)
                    previousResponse = lastResponse
                    lastResponse = result.bytes
                } else {
                    append(.note, note: "No data returned")
                }
            } else {
                error = result.error
                append(.note, note: "Error: \(result.error)")
            }
        }
    }

    func close() {
        Task { await closeActive() }
    }

    func clearConsole() {
        frames.removeAll()
        lastResponse.removeAll()
        previousResponse.removeAll()
    }

    private func closeActive() async {
        guard let device = activeDevice, !handle.isEmpty else { return }
        await bridge.closeLocal(handle: handle, kind: device.kind)
        append(.note, note: "Closed \(device.label)")
        handle = ""
        activeDevice = nil
    }

    private func append(_ direction: TrafficFrame.Direction, bytes: [UInt8] = [], note: String = "") {
        frames.append(TrafficFrame(direction: direction, bytes: bytes, note: note))
        if frames.count > 500 { frames.removeFirst(frames.count - 500) }
    }
}
