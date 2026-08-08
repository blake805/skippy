import Foundation
import AVFoundation
import Combine

/// Full-duplex `/ws/voice` client: mic up at 16 kHz, playback at the server rate.
///
/// The iOS twist relative to the Mac version is AVAudioSession: without the
/// `.playAndRecord` + `.voiceChat` configuration the phone either records
/// nothing or plays through the earpiece at whisper volume. Echo cancellation
/// is separate: the session mode alone does not give a raw AVAudioEngine the
/// voice-processing unit — that is opted into on the input node in
/// `startAudio`, and it is what makes barge-in usable on a device whose mic
/// sits an inch from its speaker.
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
    /// The player's connection format: mono float at the voice unit's rate.
    /// Server audio is resampled into this before scheduling.
    private var playerFormat: AVAudioFormat?
    private var started = false
    /// Whether Apple's echo canceller is actually running. When it is not
    /// (the VP unit failed and the raw fallback is in use), the mic is muted
    /// while Skippy speaks on the loudspeaker, so his own voice cannot trip
    /// the hub's barge-in.
    private var vpActive = false

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
        switch AVAudioApplication.shared.recordPermission {
        case .undetermined:
            AVAudioApplication.requestRecordPermission { [weak self] ok in
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
        } catch {
            state = "audio error: \(error.localizedDescription)"
            return
        }

        do {
            // Apple's voice-processing I/O: acoustic echo cancellation, noise
            // suppression, gain control. The `.voiceChat` session mode does
            // not grant this to a raw AVAudioEngine — without the explicit
            // opt-in the speaker's audio loops into the mic and Skippy's own
            // voice trips the hub's barge-in mid-sentence.
            try startEngine(voiceProcessing: true)
        } catch {
            // The VP unit failed to initialize or capture. Voice must not die
            // with it: fall back to the raw mic, where the hub's
            // sustained-speech barge-in still guards against echo.
            do {
                try startEngine(voiceProcessing: false)
            } catch {
                state = "audio error: \(error.localizedDescription)"
            }
        }
    }

    private enum AudioSetupError: Error, LocalizedError {
        case deadInputFormat
        var errorDescription: String? { "the input device reports no usable format" }
    }

    /// Build and start the whole graph, in the order the voice-processing
    /// unit demands (Apple's AVEchoTouch sample): a fully stopped engine, the
    /// VP toggle before any format is read (it changes them), and playback at
    /// the voice unit's own rate rather than a fixed lane — the VP unit
    /// delivers silent mic buffers when its render graph is not shaped the
    /// way it expects.
    private func startEngine(voiceProcessing: Bool) throws {
        engine.stop()
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        if input.isVoiceProcessingEnabled != voiceProcessing {
            try input.setVoiceProcessingEnabled(voiceProcessing)
        }

        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioSetupError.deadInputFormat
        }
        let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                  channels: 1, interleaved: true)!
        converter = AVAudioConverter(from: format, to: target)

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            Task { @MainActor in
                // Metering runs whether or not we transmit: the orb and
                // waveform should breathe whenever the mic is hot, and a
                // dead display during push-to-talk-idle reads as "broken".
                self.meter(buffer)
                if self.pushToTalk && !self.transmitting { return }
                // No echo canceller and playing through the loudspeaker: what
                // the mic hears while Skippy talks is mostly Skippy. Send
                // nothing, so nothing can cut him off.
                if !self.vpActive, self.state == "speaking", Self.onLoudspeaker() { return }
                self.sendMic(buffer)
            }
        }

        let playback = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.sampleRate,
                                     channels: 1, interleaved: false)!
        playerFormat = playback
        if player.engine == nil { engine.attach(player) }
        engine.connect(player, to: engine.mainMixerNode, format: playback)
        engine.connect(engine.mainMixerNode, to: engine.outputNode,
                       format: engine.outputNode.outputFormat(forBus: 0))
        engine.prepare()
        try engine.start()
        player.play()
        vpActive = input.isVoiceProcessingEnabled
        started = true
        state = "listening"
    }

    private static func onLoudspeaker() -> Bool {
        AVAudioSession.sharedInstance().currentRoute.outputs
            .contains { $0.portType == .builtInSpeaker }
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
