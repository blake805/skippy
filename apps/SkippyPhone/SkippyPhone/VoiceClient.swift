import Foundation
import AVFoundation
import Combine

/// Full-duplex `/ws/voice` client: mic up at 16 kHz, playback at the server rate.
///
/// The iOS twist relative to the Mac version is AVAudioSession: without the
/// `.playAndRecord` + `.voiceChat` configuration the phone either records
/// nothing or plays through the earpiece at whisper volume. `.voiceChat` also
/// enables the hardware echo canceller, which is what makes barge-in usable on
/// a device whose mic sits an inch from its speaker.
@MainActor
final class VoiceClient: ObservableObject {
    @Published var connected = false
    @Published var state: String = "idle"
    @Published var transcript: String = ""
    @Published var reply: String = ""
    @Published var metrics = VoiceMetrics()
    @Published var pushToTalk = false
    @Published var transmitting = false
    /// How many bars the live waveform keeps; roughly one second of mic audio.
    static let waveformBars = 48
    /// Smoothed mic level, 0…1, for the orb's breathing.
    @Published var level: Float = 0
    /// Recent levels, newest last, for the live waveform.
    @Published var waveform: [Float] = Array(repeating: 0, count: VoiceClient.waveformBars)

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
        guard transmitting != down else { return }
        transmitting = down
        if down { Haptics.tap() } else { Haptics.shift() }
        if down, !started { startAudio() }
    }

    func ensureListening() {
        if !started { startAudio() }
    }

    private func startAudio() {
        let session = AVAudioSession.sharedInstance()
        switch session.recordPermission {
        case .undetermined:
            session.requestRecordPermission { [weak self] ok in
                if ok { Task { @MainActor in self?.startAudio() } }
            }
            return
        case .denied:
            state = "mic denied — enable in iOS Settings"
            return
        default:
            break
        }

        do {
            try session.setCategory(
                .playAndRecord,
                mode: .voiceChat,
                options: [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP]
            )
            try session.setActive(true)

            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                      channels: 1, interleaved: true)!
            converter = AVAudioConverter(from: format, to: target)

            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                guard let self else { return }
                Task { @MainActor in
                    // Metering runs whether or not we transmit: the orb and
                    // waveform should breathe whenever the mic is hot, and a
                    // dead display during push-to-talk-idle reads as "broken".
                    self.meter(buffer)
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
        } catch {
            state = "audio error: \(error.localizedDescription)"
        }
    }

    private func stopAudio() {
        engine.inputNode.removeTap(onBus: 0)
        player.stop()
        engine.stop()
        started = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// RMS of one mic buffer, smoothed into `level` and appended to the
    /// waveform history. Attack is instant and decay eased, which is what
    /// makes the orb feel like it is listening rather than flickering.
    private func meter(_ buffer: AVAudioPCMBuffer) {
        guard let channel = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        var sum: Float = 0
        for index in 0..<Int(buffer.frameLength) {
            let sample = channel[index]
            sum += sample * sample
        }
        let rms = (sum / Float(buffer.frameLength)).squareRoot()
        // Speech RMS on an iPhone mic sits around 0.02–0.2; scale into 0…1.
        let scaled = min(rms * 6, 1)
        level = scaled > level ? scaled : level * 0.82
        waveform.removeFirst()
        waveform.append(level)
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
        guard error == nil, let channel = out.int16ChannelData?[0] else { return }
        let data = Data(bytes: channel, count: Int(out.frameLength) * 2)
        socket.sendBinary(data)
    }

    private func handleJSON(_ msg: [String: Any]) {
        let type = msg["type"] as? String ?? ""
        switch type {
        case "state":
            let next = msg["state"] as? String ?? state
            // Feel the turn-taking: a nudge when Skippy starts talking and a
            // lighter one when it hands the floor back.
            if next != state {
                if next == "speaking" { Haptics.shift() }
                else if next == "listening", state == "speaking" { Haptics.tap() }
            }
            state = next
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
