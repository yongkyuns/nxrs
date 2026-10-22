import SwiftUI

@main @MainActor struct NxrsMonitorApp: App {
    @StateObject private var model: MonitorModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--design-preview") {
            _model = StateObject(wrappedValue: MonitorModel.designPreview())
        } else if ProcessInfo.processInfo.arguments.contains("--onboarding-preview") {
            _model = StateObject(wrappedValue: MonitorModel.onboardingPreview())
        } else { _model = StateObject(wrappedValue: MonitorModel()) }
        #else
        _model = StateObject(wrappedValue: MonitorModel())
        #endif
    }

    var body: some Scene {
        WindowGroup {
            MonitorView(model: model)
                .task { model.restore() }
                .onChange(of: scenePhase) { phase in model.setForeground(phase == .active) }
        }
    }
}
