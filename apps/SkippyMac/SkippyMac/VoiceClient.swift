import Foundation
import AVFoundation
import Combine
import CoreAudio
import os.log

/// Capture-path telemetry, readable with:
///   log stream --predicate 'subsystem == "com.blake.SkippyMac"'
private let voiceLog = Logger(subsystem: "com.blake.SkippyMac", category: "voice")

/// Full-duplex `/ws/voice` client: mic up at 16 kHz, playback at the server rate.
@MainActor
final class VoiceClient: ObservableObject {
    @Published var connected = false
    @Published var state: String = "idle"
    @Published var transcript: String = ""
    @Published var reply: String = ""
    @Published var metrics = VoiceMetrics()
    @Published var pushToTalk = false
    @Published var transmitting = false

    private let socket = WebSocketSession()
    private var settings: SettingsStore
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var outRate: Double = 24_000
    private var converter: AVAudioConverter?
    /// The player's connection format: mono float at the input hardware rate.
    /// Server audio is resampled into this before scheduling.
    private var playerFormat: AVAudioFormat?
    private var started = false
    /// True when playback can leak into the mic (anything but Bluetooth
    /// headphones). While Skippy speaks, the mic is muted rather than
    /// cancelled: Apple's voice-processing unit delivers pure silence on this
    /// machine (its aggregate device chokes on virtual audio devices like
    /// Background Music), so echo is kept out of the wire by not sending
    /// during playback. On AirPods the earbuds cancel echo themselves and
    /// voice barge-in stays fully live.
    private var echoGate = true

    init(settings: SettingsStore) {
        self.settings = settings
        socket.onStateChange = { [weak self] ok in
            Task { @MainActor in
                self?.connected = ok
                if ok { self?.socket.sendJSON(["type": "start", "duplex": true]) }
            }
        }
        socket.onMessage = { [weak self] msg in
            Task { @MainActor in self?.handleJSON(msg) }
        }
        socket.onBinary = { [weak self] data in
            Task { @MainActor in self?.play(data) }
        }
    }

    func connect() {
        socket.connect(to: settings.voiceURL)
    }

    func disconnect() {
        stopAudio()
        socket.sendJSON(["type": "end"])
        socket.disconnect()
        state = "idle"
    }

    func updateSettings(_ settings: SettingsStore) {
        self.settings = settings
    }

    func interrupt() {
        socket.sendJSON(["type": "interrupt"])
        player.stop()
    }

    func setPushToTalk(_ down: Bool) {
        transmitting = down
        if down, !started { startAudio() }
    }

    func ensureListening() {
        if !started { startAudio() }
    }

    private var sentBytes = 0

    private func startAudio() {
        let session = AVCaptureDevice.authorizationStatus(for: .audio)
        if session == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] ok in
                if ok { Task { @MainActor in self?.startAudio() } }
            }
            return
        }
        guard session == .authorized else {
            state = "mic denied"
            return
        }

        do {
            try startEngine()
        } catch {
            voiceLog.error("startAudio: engine failed: \(error.localizedDescription, privacy: .public)")
            state = "audio error: \(error.localizedDescription)"
        }
    }

    private enum AudioSetupError: Error, LocalizedError {
        case deadInputFormat
        var errorDescription: String? { "the input device reports no usable format" }
    }

    /// Build and start the raw capture/playback graph.
    ///
    /// Deliberately NOT Apple's voice-processing unit: on this Mac it starts
    /// but captures pure digital silence (peakRms=0 across a full session) —
    /// its aggregate device does not survive virtual audio devices such as
    /// Background Music. Echo is handled by the gate in the tap instead.
    private func startEngine() throws {
        engine.stop()
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        if input.isVoiceProcessingEnabled {
            try? input.setVoiceProcessingEnabled(false)
        }
        echoGate = !Self.outputIsBluetooth()

        let format = input.outputFormat(forBus: 0)
        voiceLog.info("startEngine: input rate=\(format.sampleRate, privacy: .public) ch=\(format.channelCount, privacy: .public) echoGate=\(self.echoGate, privacy: .public)")
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioSetupError.deadInputFormat
        }
        let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                  channels: 1, interleaved: true)!
        converter = AVAudioConverter(from: format, to: target)
        if converter == nil {
            voiceLog.error("startEngine: AVAudioConverter is nil — capture cannot run")
        }

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            Task { @MainActor in
                if self.pushToTalk && !self.transmitting { return }
                // The echo gate: on speakers, what the mic hears while Skippy
                // talks is mostly Skippy. Nothing is sent, so nothing can trip
                // the hub's barge-in. Interrupt by button, or by voice on
                // Bluetooth headphones where the earbuds cancel echo.
                if self.echoGate && self.state == "speaking" { return }
                self.sendMic(buffer)
            }
        }

        let playback = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.sampleRate,
                                     channels: 1, interleaved: false)!
        playerFormat = playback
        if player.engine == nil { engine.attach(player) }
        engine.connect(player, to: engine.mainMixerNode, format: playback)
        engine.prepare()
        try engine.start()
        player.play()
        started = true
        state = "listening"
        voiceLog.info("startEngine: running")
    }

    /// True when the default output is a Bluetooth device (AirPods and kin),
    /// which cancel echo in their own hardware. Everything else — built-in
    /// speakers, and virtual devices like Background Music that may feed them
    /// — gets the gate.
    private static func outputIsBluetooth() -> Bool {
        var deviceID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID
        ) == noErr, deviceID != 0 else { return false }

        var transport = UInt32(0)
        size = UInt32(MemoryLayout<UInt32>.size)
        address.mSelector = kAudioDevicePropertyTransportType
        guard AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &transport) == noErr else {
            return false
        }
        return transport == kAudioDeviceTransportTypeBluetooth
            || transport == kAudioDeviceTransportTypeBluetoothLE
    }

    private func stopAudio() {
        engine.inputNode.removeTap(onBus: 0)
        player.stop()
        engine.stop()
        started = false
    }

    private func sendMic(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let ratio = 16_000 / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else { return }
        var error: NSError?
        var consumed = false
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                // .noDataNow, never .endOfStream: the converter is reused for
                // every mic buffer, and it latches EOF permanently — returning
                // .endOfStream here made it emit one buffer and then silence
                // for the rest of the session.
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        if let error {
            if sentBytes == 0 { voiceLog.error("sendMic: convert error: \(error.localizedDescription, privacy: .public)") }
            return
        }
        guard let channel = out.int16ChannelData?[0] else { return }
        let frames = Int(out.frameLength)
        guard frames > 0 else { return }
        let data = Data(bytes: channel, count: frames * 2)
        sentBytes += data.count
        socket.sendBinary(data)
    }

    private func handleJSON(_ msg: [String: Any]) {
        let type = msg["type"] as? String ?? ""
        switch type {
        case "state":
            state = msg["state"] as? String ?? state
            if state == "listening" { ensureListening() }
        case "transcript":
            transcript = msg["text"] as? String ?? ""
        case "reply":
            let text = msg["text"] as? String ?? ""
            if reply.isEmpty { reply = text } else { reply += " " + text }
        case "audio_start":
            if let rate = msg["rate"] as? Int { outRate = Double(rate) }
            // The route can change mid-session (AirPods in, AirPods out);
            // re-decide the echo gate at every playback start.
            echoGate = !Self.outputIsBluetooth()
            reply = ""
        case "audio_end":
            break
        case "audio_cancel":
            player.stop()
            player.play()
        case "metrics":
            metrics = VoiceMetrics(
                sttMs: msg["stt_ms"] as? Int ?? 0,
                firstTokenMs: msg["llm_first_token_ms"] as? Int ?? 0,
                firstAudioMs: msg["first_audio_ms"] as? Int ?? 0,
                totalMs: msg["total_ms"] as? Int ?? 0
            )
        case "error":
            state = msg["message"] as? String ?? "error"
        default:
            break
        }
    }

    private func play(_ data: Data) {
        guard !data.isEmpty else { return }
        if !started { startAudio() }
        guard let playback = playerFormat else { return }
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: outRate,
                                   channels: 1, interleaved: true)!
        let frames = UInt32(data.count / 2)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        buffer.frameLength = frames
        data.withUnsafeBytes { raw in
            if let src = raw.bindMemory(to: Int16.self).baseAddress,
               let dst = buffer.int16ChannelData?[0] {
                dst.update(from: src, count: Int(frames))
            }
        }
        // Convert to the player's format: float, and the voice unit's sample
        // rate rather than the server's, so the capacity scales by the ratio.
        let capacity = AVAudioFrameCount(Double(frames) * playback.sampleRate / outRate) + 32
        guard let floatBuf = AVAudioPCMBuffer(pcmFormat: playback, frameCapacity: capacity),
              let converter = AVAudioConverter(from: format, to: playback) else {
            return
        }
        var error: NSError?
        var consumed = false
        converter.convert(to: floatBuf, error: &error) { _, status in
            if consumed { status.pointee = .endOfStream; return nil }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, floatBuf.frameLength > 0 else { return }
        player.scheduleBuffer(floatBuf, completionHandler: nil)
        if !player.isPlaying { player.play() }
    }
}
