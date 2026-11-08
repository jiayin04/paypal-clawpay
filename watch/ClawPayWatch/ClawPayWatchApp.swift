import SwiftUI
import AppIntents

@main
struct ClawPayWatchApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .task { ClawPayShortcuts.updateAppShortcutParameters() }
        }
    }
}
