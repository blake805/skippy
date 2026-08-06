import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        // Bindings must run through the ObservableObject store itself, not the
        // `let app.settings`: a writable key path cannot pass through a constant.
        SettingsForm(settings: app.settings, factory: app.factory) { app.connectAll() }
    }
}

struct SettingsForm: View {
    @ObservedObject var settings: SettingsStore
    @ObservedObject var factory: FactoryClient
    let reconnect: () -> Void
    /// The PAT lives here only until Connect is clicked; the hub stores it,
    /// so the app never persists it anywhere.
    @State private var githubToken = ""

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
            Section("Bench node") {
                SecureField("Node token (SKIPPY_TOKEN)", text: $settings.benchToken)
                Text("The token flashed into the io-node's secrets.h. The Bluetooth bridge in the RE dashboard proves itself with it; leave empty if the node has none.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("GitHub") {
                if factory.githubStatus?.connected == true {
                    HStack {
                        Image(systemName: "checkmark.seal.fill")
                            .foregroundStyle(.green)
                        Text(factory.githubStatus?.headline ?? "Connected")
                        Spacer()
                        Button("Disconnect") {
                            factory.setGitHubToken("")
                            githubToken = ""
                        }
                    }
                } else {
                    SecureField("Personal access token", text: $githubToken)
                        .onSubmit { connectGitHub() }
                    HStack {
                        Text(factory.githubStatus?.error
                             ?? "Stored on the hub, never in this app. A fine-grained PAT scoped to the repos Skippy should touch is plenty.")
                            .font(.caption)
                            .foregroundStyle(factory.githubStatus?.error == nil ? .secondary : Color.red)
                        Spacer()
                        Button("Connect") { connectGitHub() }
                            .disabled(githubToken.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
            Section {
                Button("Reconnect") { reconnect() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .formStyle(.grouped)
        .padding()
        .navigationTitle("Settings")
        .onAppear { factory.requestGitHubStatus() }
    }

    private func connectGitHub() {
        let token = githubToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return }
        factory.setGitHubToken(token)
        githubToken = ""
    }
}
