import SwiftUI

struct VoiceView: View {
    @EnvironmentObject private var app: AppModel
    @ObservedObject var voice: VoiceClient

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            ZStack {
                Circle()
                    .fill(Color.accentColor.opacity(voice.state == "speaking" ? 0.25 : 0.08))
                    .frame(width: 180, height: 180)
                    .scaleEffect(voice.state == "speaking" ? 1.08 : 1.0)
                    .animation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true),
                               value: voice.state == "speaking")
                Image(systemName: "waveform.circle.fill")
                    .font(.system(size: 72))
                    .foregroundStyle(Color.accentColor)
            }
            Text(voice.state.capitalized)
                .font(.title2.weight(.medium))
            if !voice.transcript.isEmpty {
                Text("You: \(voice.transcript)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            if !voice.reply.isEmpty {
                Text(voice.reply)
                    .font(.title3)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
                    .textSelection(.enabled)
            }
            metrics
            Spacer()
            controls
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            voice.connect()
            voice.ensureListening()
        }
    }

    private var metrics: some View {
        HStack(spacing: 16) {
            metric("STT", voice.metrics.sttMs)
            metric("Token", voice.metrics.firstTokenMs)
            metric("Audio", voice.metrics.firstAudioMs)
            metric("Total", voice.metrics.totalMs)
        }
        .font(.caption.monospacedDigit())
        .foregroundStyle(.secondary)
    }

    private func metric(_ label: String, _ ms: Int) -> some View {
        VStack(spacing: 2) {
            Text(label)
            Text("\(ms) ms")
        }
    }

    private var controls: some View {
        VStack(spacing: 14) {
            if voice.pushToTalk {
                // Hold-to-talk replaces the Mac's spacebar: press-and-hold is
                // the natural phone gesture for the same half-duplex intent.
                Text(voice.transmitting ? "Transmitting…" : "Hold to talk")
                    .font(.headline)
                    .foregroundStyle(voice.transmitting ? Color.accentColor : .secondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 22)
                    .background(Color.accentColor.opacity(voice.transmitting ? 0.25 : 0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .padding(.horizontal, 24)
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { _ in if !voice.transmitting { voice.setPushToTalk(true) } }
                            .onEnded { _ in voice.setPushToTalk(false) }
                    )
            }
            HStack(spacing: 20) {
                Toggle("Push to talk", isOn: $voice.pushToTalk)
                    .fixedSize()
                Button("Interrupt") { voice.interrupt() }
                    .buttonStyle(.bordered)
                Button(voice.connected ? "Connected" : "Connect") {
                    voice.connect()
                }
                .disabled(voice.connected)
            }
        }
        .padding(.bottom, 20)
    }
}
