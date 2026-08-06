import SwiftUI

@main
struct SkippyMacApp: App {
    @StateObject private var appModel = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appModel)
                .frame(minWidth: 980, minHeight: 640)
                // A shop cockpit, not a document: dark regardless of the
                // system setting. Theme draws in semantic colors, so this one
                // line restyles every card and pill.
                .preferredColorScheme(.dark)
        }
        .defaultSize(width: 1180, height: 760)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Skippy") {
                Button(appModel.factory.connected ? "Reconnect Hub" : "Connect Hub") {
                    appModel.connectAll()
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                Divider()
                Button("Cancel Current Run") {
                    appModel.cancelRun()
                }
                .keyboardShortcut(".", modifiers: [.command])
                .disabled(!appModel.isRunning)
            }
        }

        Settings {
            SettingsView()
                .environmentObject(appModel)
                .frame(width: 480, height: 560)
                .preferredColorScheme(.dark)
        }
    }
}
