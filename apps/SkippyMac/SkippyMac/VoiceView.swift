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
                    .padding(.horizontal, 40)
            }
            if !voice.reply.isEmpty {
                Text(voice.reply)
                    .font(.title3)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 48)
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
        HStack(spacing: 20) {
            Toggle("Push to talk", isOn: $voice.pushToTalk)
                .toggleStyle(.switch)
            if voice.pushToTalk {
                Text("Hold space")
                    .foregroundStyle(.secondary)
            }
            Button("Interrupt") { voice.interrupt() }
                .buttonStyle(.bordered)
            Button(voice.connected ? "Connected" : "Connect") {
                voice.connect()
            }
            .disabled(voice.connected)
        }
        .padding(.bottom, 28)
        .background(KeyCatcher(isEnabled: voice.pushToTalk) { down in
            voice.setPushToTalk(down)
        })
    }
}

/// Tiny NSView that reports spacebar up/down for push-to-talk.
struct KeyCatcher: NSViewRepresentable {
    let isEnabled: Bool
    let onSpace: (Bool) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = KeyView()
        view.onSpace = onSpace
        view.isEnabled = isEnabled
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        guard let view = nsView as? KeyView else { return }
        view.onSpace = onSpace
        view.isEnabled = isEnabled
    }

    final class KeyView: NSView {
        var onSpace: ((Bool) -> Void)?
        var isEnabled = false
        override var acceptsFirstResponder: Bool { true }
        override func keyDown(with event: NSEvent) {
            if isEnabled, event.keyCode == 49 { onSpace?(true) } else { super.keyDown(with: event) }
        }
        override func keyUp(with event: NSEvent) {
            if isEnabled, event.keyCode == 49 { onSpace?(false) } else { super.keyUp(with: event) }
        }
    }
}
