import Foundation
import AVFoundation
import Combine
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
    private var started = false

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

    private var tapCount = 0
    private var sentBytes = 0

    private func startAudio() {
        let session = AVCaptureDevice.authorizationStatus(for: .audio)
        voiceLog.info("startAudio: auth=\(session.rawValue, privacy: .public) started=\(self.started, privacy: .public)")
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
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            voiceLog.info("startAudio: input format rate=\(format.sampleRate, privacy: .public) ch=\(format.channelCount, privacy: .public)")
            let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                      channels: 1, interleaved: true)!
            converter = AVAudioConverter(from: format, to: target)
            if converter == nil {
                voiceLog.error("startAudio: AVAudioConverter is nil — capture cannot run")
            }

            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                guard let self else { return }
                Task { @MainActor in
                    self.tapCount += 1
                    if self.tapCount % 200 == 1 {
                        voiceLog.info("tap fired: count=\(self.tapCount, privacy: .public) sentBytes=\(self.sentBytes, privacy: .public) connected=\(self.connected, privacy: .public)")
                    }
                    if self.pushToTalk && !self.transmitting { return }
                    self.sendMic(buffer)
                }
            }

            if player.engine == nil {
                engine.attach(player)
                let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate,
                                              channels: 1, interleaved: false)!
                engine.connect(player, to: engine.mainMixerNode, format: outFormat)
            }
            try engine.start()
            player.play()
            started = true
            state = "listening"
            voiceLog.info("startAudio: engine running")
        } catch {
            voiceLog.error("startAudio: engine failed: \(error.localizedDescription, privacy: .public)")
            state = "audio error: \(error.localizedDescription)"
        }
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
        let data = Data(bytes: channel, count: Int(out.frameLength) * 2)
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
        // Convert to float for the player node graph.
        let floatFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate,
                                        channels: 1, interleaved: false)!
        guard let floatBuf = AVAudioPCMBuffer(pcmFormat: floatFormat, frameCapacity: frames),
              let converter = AVAudioConverter(from: format, to: floatFormat) else {
            return
        }
        floatBuf.frameLength = frames
        var error: NSError?
        var consumed = false
        converter.convert(to: floatBuf, error: &error) { _, status in
            if consumed { status.pointee = .endOfStream; return nil }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        if !started { startAudio() }
        player.scheduleBuffer(floatBuf, completionHandler: nil)
        if !player.isPlaying { player.play() }
    }
}
