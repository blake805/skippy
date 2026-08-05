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
        Form {
            Section("Hub") {
                TextField("Host", text: $settings.hubHost)
                TextField("Port", value: $settings.hubPort, format: .number)
                TextField("Client ID", text: $settings.clientId)
                Text("Factory: \(settings.factoryURL.absoluteString)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Section("Voice") {
                SecureField("Voice token (SKIPPY_VOICE_TOKEN)", text: $settings.voiceToken)
                Text("Leave empty if the hub has no token set.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Devices") {
                Toggle("Share this Mac's serial/network devices with Skippy", isOn: $settings.shareDevices)
                Text("When on, RE mode can use host=macbook for parts plugged in here. Writes still require your approval.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Button("Reconnect") { reconnect() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .formStyle(.grouped)
        .padding()
        .navigationTitle("Settings")
    }
}
