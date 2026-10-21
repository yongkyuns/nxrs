import SwiftUI

@main struct NxrsMonitorApp: App {
    @StateObject private var model = MonitorModel()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            MonitorView(model: model)
                .task { model.restore() }
                .onChange(of: scenePhase) { phase in model.setForeground(phase == .active) }
        }
    }
}
