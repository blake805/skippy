import SwiftUI

/// The voice-first screen: an orb that breathes with the mic, a live
/// waveform, the running transcript and reply, and the hands-free /
/// push-to-talk switch. This is the screen the phone exists for — everything
/// on it is reachable with a thumb.
struct VoiceView: View {
    @EnvironmentObject private var app: AppModel
    @ObservedObject var voice: VoiceClient

    var body: some View {
        VStack(spacing: 18) {
            statusHeader
            Spacer(minLength: 0)
            VoiceOrb(state: voice.state, level: voice.level)
            WaveformView(
                samples: voice.waveform,
                active: !voice.pushToTalk || voice.transmitting
            )
            .frame(height: 44)
            .padding(.horizontal, 40)
            conversation
            Spacer(minLength: 0)
            metrics
            controls
        }
        .padding(.vertical, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            voice.connect()
            voice.ensureListening()
        }
    }

    private var statusHeader: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(voice.connected ? Color.green : Color.orange)
                .frame(width: 8, height: 8)
            Text(stateLabel)
                .font(.headline)
            Spacer()
            Button {
                voice.interrupt()
                Haptics.tap()
            } label: {
                Label("Interrupt", systemImage: "stop.circle")
                    .font(.callout)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(voice.state != "speaking")
        }
        .padding(.horizontal, 20)
    }

    private var stateLabel: String {
        switch voice.state {
        case "listening": return voice.pushToTalk && !voice.transmitting
            ? "Ready — hold to talk" : "Listening"
        case "thinking": return "Thinking…"
        case "speaking": return "Speaking"
        case "idle": return voice.connected ? "Ready" : "Connecting…"
        default: return voice.state
        }
    }

    @ViewBuilder
    private var conversation: some View {
        VStack(spacing: 10) {
            if !voice.transcript.isEmpty {
                Text(voice.transcript)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .card(accent: Theme.voice)
            }
            if !voice.reply.isEmpty {
                ScrollView {
                    Text(voice.reply)
                        .font(.body)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 150)
                .card()
            }
        }
        .padding(.horizontal, 20)
        .animation(.easeInOut(duration: 0.2), value: voice.transcript)
        .animation(.easeInOut(duration: 0.2), value: voice.reply)
    }

    private var metrics: some View {
        HStack(spacing: 16) {
            metric("STT", voice.metrics.sttMs)
            metric("Token", voice.metrics.firstTokenMs)
            metric("Audio", voice.metrics.firstAudioMs)
            metric("Total", voice.metrics.totalMs)
        }
        .font(.caption2.monospacedDigit())
        .foregroundStyle(.tertiary)
    }

    private func metric(_ label: String, _ ms: Int) -> some View {
        VStack(spacing: 1) {
            Text(label)
            Text("\(ms) ms")
        }
    }

    private var controls: some View {
        VStack(spacing: 12) {
            Picker("Talk mode", selection: $voice.pushToTalk) {
                Text("Hands-free").tag(false)
                Text("Push to talk").tag(true)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 24)

            if voice.pushToTalk {
                // Hold-to-talk replaces the Mac's spacebar: press-and-hold is
                // the natural phone gesture for the same half-duplex intent.
                Label(
                    voice.transmitting ? "Transmitting…" : "Hold to talk",
                    systemImage: voice.transmitting ? "dot.radiowaves.left.and.right" : "mic.fill"
                )
                .font(.headline)
                .foregroundStyle(voice.transmitting ? Theme.voice : .secondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 22)
                .background(Theme.voice.opacity(voice.transmitting ? 0.25 : 0.10))
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .padding(.horizontal, 24)
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { _ in if !voice.transmitting { voice.setPushToTalk(true) } }
                        .onEnded { _ in voice.setPushToTalk(false) }
                )
            }
        }
        .padding(.bottom, 6)
    }
}

/// The voice orb: a gradient core that scales with the live mic level while
/// listening, pulses while speaking, and sits dim while idle. Two soft rings
/// trail the core so loud moments bloom outward.
struct VoiceOrb: View {
    let state: String
    /// Live mic level, 0…1.
    let level: Float

    @State private var speakingPulse = false

    private var breathing: CGFloat {
        state == "listening" ? CGFloat(level) : 0
    }

    var body: some View {
        ZStack {
            Circle()
                .fill(Theme.voice.opacity(0.08))
                .frame(width: 190, height: 190)
                .scaleEffect(1 + breathing * 0.35)
            Circle()
                .fill(Theme.voice.opacity(0.14))
                .frame(width: 150, height: 150)
                .scaleEffect(1 + breathing * 0.22)
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Theme.voice.opacity(0.85), Theme.voice.opacity(0.45)],
                        center: .center, startRadius: 6, endRadius: 62
                    )
                )
                .frame(width: 110, height: 110)
                .scaleEffect(coreScale)
                .animation(
                    state == "speaking"
                        ? .easeInOut(duration: 0.55).repeatForever(autoreverses: true)
                        : .easeOut(duration: 0.1),
                    value: state == "speaking" ? speakingPulse : (breathing > 0.4)
                )
            Image(systemName: icon)
                .font(.system(size: 40, weight: .medium))
                .foregroundStyle(.white)
                .symbolEffect(.pulse, isActive: state == "thinking")
        }
        .animation(.easeOut(duration: 0.12), value: breathing)
        .onChange(of: state) { _, next in
            speakingPulse = next == "speaking"
        }
        .onAppear { speakingPulse = state == "speaking" }
    }

    private var coreScale: CGFloat {
        if state == "speaking" { return speakingPulse ? 1.10 : 1.0 }
        return 1 + breathing * 0.12
    }

    private var icon: String {
        switch state {
        case "speaking": return "waveform"
        case "thinking": return "ellipsis"
        case "listening": return "mic.fill"
        default: return "waveform.circle"
        }
    }
}

/// A rolling bar waveform of the last second of mic audio. Dimmed when
/// push-to-talk is idle, so a hot-but-muted mic reads as standing by rather
/// than transmitting.
struct WaveformView: View {
    let samples: [Float]
    let active: Bool

    var body: some View {
        GeometryReader { geo in
            let barWidth = geo.size.width / CGFloat(max(samples.count, 1))
            HStack(alignment: .center, spacing: barWidth * 0.35) {
                ForEach(Array(samples.enumerated()), id: \.offset) { _, sample in
                    Capsule()
                        .fill(Theme.voice.opacity(active ? 0.7 : 0.25))
                        .frame(
                            width: barWidth * 0.65,
                            height: max(3, CGFloat(sample) * geo.size.height)
                        )
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
        }
    }
}
