import SwiftUI

private enum HomeTab: Hashable { case home, activity, library, settings }

struct MonitorView: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject private var settings: HomeSettings
    @State private var tab: HomeTab = .home

    init(model: MonitorModel) {
        self.model = model
        self.settings = model.settings
    }

    var body: some View {
        Group {
            if model.enrolled { tabs }
            else { HomeSetupView(model: model, replacing: false) }
        }
        .tint(HomeTheme.forest)
        .preferredColorScheme(settings.colorScheme)
        .overlay(alignment: .top) {
            if let toast = model.toast {
                Text(toast).font(.subheadline.weight(.medium))
                    .foregroundStyle(HomeTheme.ink).padding(.horizontal, 20).padding(.vertical, 14)
                    .background(HomeTheme.surface, in: Capsule())
                    .overlay(Capsule().strokeBorder(HomeTheme.line, lineWidth: 1))
                    .padding(.horizontal, 20).padding(.top, 8)
                    .accessibilityAddTraits(.updatesFrequently)
                    .accessibilityIdentifier("home-toast")
                    .allowsHitTesting(false)
            }
        }
        .onChange(of: settings.preferences.keepScreenAwake) { _ in model.updateDisplayAwake() }
    }

    @ViewBuilder private var tabs: some View {
        if #available(iOS 18, *) {
            TabView(selection: $tab) {
                Tab("Home", systemImage: "house", value: .home) { HomeDashboard(model: model, settings: settings) }
                Tab("Activity", systemImage: "clock", value: .activity) { activity }
                Tab("Recordings", systemImage: "film.stack", value: .library) { library }
                Tab("Settings", systemImage: "slider.horizontal.3", value: .settings) { preferences }
            }
        } else {
            TabView(selection: $tab) {
                HomeDashboard(model: model, settings: settings)
                    .tabItem { Label("Home", systemImage: "house") }.tag(HomeTab.home)
                activity
                    .tabItem { Label("Activity", systemImage: "clock") }.tag(HomeTab.activity)
                library
                    .tabItem { Label("Recordings", systemImage: "film.stack") }.tag(HomeTab.library)
                preferences
                    .tabItem { Label("Settings", systemImage: "slider.horizontal.3") }.tag(HomeTab.settings)
            }
        }
    }

    private var library: some View {
        HomeLibraryView(library: model.library).safeAreaInset(edge: .bottom) { viewingBar }
    }
    private var activity: some View {
        HomeActivityView(model: model).safeAreaInset(edge: .bottom) { viewingBar }
    }
    private var preferences: some View {
        HomeSettingsView(model: model, settings: settings).safeAreaInset(edge: .bottom) { viewingBar }
    }
    @ViewBuilder private var viewingBar: some View {
        if model.live || model.isRecording {
            HStack(spacing: 12) {
                Button { tab = .home } label: {
                    Label(model.isRecording ? "Recording camera clip" : "Camera is playing",
                          systemImage: model.isRecording ? "record.circle.fill" : "video.fill")
                        .font(.subheadline.weight(.medium)).frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain)
                Button(model.isRecording ? "Stop clip" : "Pause") {
                    if model.isRecording { model.toggleRecording() } else { model.stop() }
                }.font(.subheadline.weight(.semibold)).frame(minHeight: 44)
            }.foregroundStyle(HomeTheme.ink).padding(.horizontal, 18).padding(.vertical, 4)
                .background(HomeTheme.surface)
        }
    }
}
