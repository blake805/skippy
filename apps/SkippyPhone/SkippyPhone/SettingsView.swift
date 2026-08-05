import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        // Bindings must run through the ObservableObject store itself, not the
        // `let app.settings`: a writable key path cannot pass through a constant.
        SettingsForm(settings: app.settings) { app.connectAll() }
    }
}

struct SettingsForm: View {
    @ObservedObject var settings: SettingsStore
    let reconnect: () -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("Hub") {
                    TextField("Host", text: $settings.hubHost)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                    TextField("Port", value: $settings.hubPort, format: .number)
                        .keyboardType(.numberPad)
                    TextField("Client ID", text: $settings.clientId)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                    Text("Factory: \(settings.factoryURL.absoluteString)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                Section("Voice") {
                    SecureField("Voice token (SKIPPY_VOICE_TOKEN)", text: $settings.voiceToken)
                    Text("Copy it from the SkippyServer app on the Mac Studio. Leave empty if the hub has no token set.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Section {
                    Button("Reconnect") { reconnect() }
                        .buttonStyle(.borderedProminent)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
