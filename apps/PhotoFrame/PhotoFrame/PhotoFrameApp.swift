import SwiftUI

@main
struct PhotoFrameApp: App {
    @StateObject private var photoFrameModel = PhotoFrameModel()
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(photoFrameModel)
                .frame(minWidth: 800, minHeight: 600)
        }
        .defaultSize(width: 1024, height: 768)
        
        Settings {
            SettingsView()
                .environmentObject(photoFrameModel)
                .frame(width: 400, height: 300)
        }
    }
}
