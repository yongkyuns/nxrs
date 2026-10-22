import SwiftUI
import NxrsMonitorCore

struct HomeSettingsView: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject var settings: HomeSettings
    @State private var showSetup = false
    @State private var showForget = false
    @State private var showDetails = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 16) {
                        HomeMark().frame(width: 62, height: 62)
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Nxrs Home").font(.title2.weight(.bold)).foregroundStyle(HomeTheme.ink)
                            Text("Private home monitoring.").font(.subheadline).foregroundStyle(HomeTheme.secondary)
                        }.padding(.vertical, 10)
                    }
                }.listRowBackground(HomeTheme.surface)
                Section("Your home") {
                    HStack {
                        Label("Home name", systemImage: "house")
                        TextField("My home", text: $settings.preferences.homeName).multilineTextAlignment(.trailing)
                            .onChange(of: settings.preferences.homeName) { value in
                                if value.count > 48 { settings.preferences.homeName = String(value.prefix(48)) }
                            }.accessibilityIdentifier("home-name")
                    }
                    LabeledContent { Text(model.gatewayHost).lineLimit(2).foregroundStyle(.secondary) }
                        label: { Label("Home address", systemImage: "network") }
                    Button { showDetails = true } label: { Label("Connection details", systemImage: "waveform.path") }
                    Button { model.reload(); model.showToast("Checking your home connection…") } label: {
                        Label(model.busy ? "Checking connection…" : "Check connection", systemImage: "arrow.clockwise")
                    }.disabled(model.busy || model.isPreview)
                }.listRowBackground(HomeTheme.surface)
                Section("Make it yours") {
                    Picker(selection: $settings.preferences.appearance) {
                        Text("Follow iPhone").tag(HomeAppearance.system)
                        Text("Light").tag(HomeAppearance.light)
                        Text("Dark").tag(HomeAppearance.dark)
                    } label: { Label("Appearance", systemImage: "circle.lefthalf.filled") }
                        .accessibilityIdentifier("appearance-picker")
                    Toggle(isOn: $settings.preferences.autoConnect) {
                        Label("Open the last camera", systemImage: "play.rectangle")
                    }
                    Toggle(isOn: $settings.preferences.keepScreenAwake) {
                        Label("Keep screen awake while live", systemImage: "sun.max")
                    }
                }.listRowBackground(HomeTheme.surface)
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        Label("Private by design", systemImage: "lock.shield").font(.headline)
                        Text("Snapshots and clips stay in this app on your iPhone and are excluded from cloud backup. Sharing is always your choice.")
                        Text("This camera provides video. Microphone, talk, pan/tilt, motion alerts and continuous recordings are not available on the current connection.")
                    }.font(.subheadline).foregroundStyle(HomeTheme.secondary).padding(.vertical, 8)
                    NavigationLink {
                        ConnectionHelpView()
                    } label: { Label("Connection help", systemImage: "questionmark.circle") }
                }.listRowBackground(HomeTheme.surface)
                Section {
                    Button { showSetup = true } label: { Label("Replace home connection", systemImage: "arrow.triangle.2.circlepath") }
                    Button(role: .destructive) { showForget = true } label: { Label("Forget this home", systemImage: "rectangle.portrait.and.arrow.right") }
                } footer: { Text("Forgetting removes this iPhone's viewer key. Saved footage remains in Recordings. It does not revoke other viewers or turn off your cameras.") }
                    .listRowBackground(HomeTheme.surface)
                Section {
                    HStack { Text("Version"); Spacer(); Text(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.2").foregroundStyle(.secondary) }
                }.listRowBackground(HomeTheme.surface)
            }.scrollContentBackground(.hidden).background(HomeTheme.canvas)
                .navigationTitle("Settings").foregroundStyle(HomeTheme.ink)
                .sheet(isPresented: $showSetup) { HomeSetupView(model: model, replacing: true) }
                .sheet(isPresented: $showDetails) { ConnectionDetailsView(model: model) }
                .confirmationDialog("Forget this home?", isPresented: $showForget, titleVisibility: .visible) {
                    Button("Forget home", role: .destructive) { model.forget() }
                } message: { Text("You'll need a new invitation or your viewer key to connect again. Your saved footage will stay on this iPhone.") }
        }
    }
}

struct CameraEditView: View {
    let camera: CameraSummary
    @ObservedObject var settings: HomeSettings
    @Environment(\.dismiss) private var dismiss
    @State private var name: String
    @State private var room: String
    @State private var favorite: Bool

    init(camera: CameraSummary, settings: HomeSettings) {
        self.camera = camera; self.settings = settings
        let value = settings.preferences.personalization(for: camera)
        self.name = value.name.isEmpty ? camera.name : value.name
        self.room = value.room
        self.favorite = value.favorite
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("A familiar name") {
                    HStack {
                        TextField("Camera name", text: $name).accessibilityIdentifier("camera-name")
                        Button { name = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).frame(width: 44, height: 44) }
                            .buttonStyle(.plain).accessibilityLabel("Clear camera name")
                    }
                    HStack {
                        TextField("Room, e.g. Living room", text: $room).accessibilityIdentifier("camera-room")
                        Button { room = "" } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).frame(width: 44, height: 44)
                        }.buttonStyle(.plain).accessibilityLabel("Clear room name")
                    }
                    Toggle("Favorite camera", isOn: $favorite)
                }
                Section {
                    Label(camera.source == .synthetic ? "Synthetic test source" : "Physical camera", systemImage: "video")
                    Text("Names and rooms are saved on this iPhone. They don't change the camera's access permissions.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
            }.navigationTitle("Camera settings").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") { settings.update(camera, name: name, room: room, favorite: favorite); dismiss() }
                            .fontWeight(.semibold).accessibilityIdentifier("save-camera-settings")
                    }
                }
        }.tint(HomeTheme.forest)
    }
}

private struct ConnectionHelpView: View {
    var body: some View {
        List {
            Section("When you're at home") {
                Label("Connect your iPhone to your home network.", systemImage: "wifi")
                Label("Check that the camera and home gateway have power.", systemImage: "powerplug")
                Label("Allow Local Network access for Nxrs Home in iPhone Settings.", systemImage: "network")
            }
            Section("When you're away") {
                Label("Connect the private-network app configured for your home.", systemImage: "lock.shield")
                Label("Check that your home gateway is awake and connected.", systemImage: "house")
            }
            Section("If a certificate cannot be verified") {
                Text("Ask the home owner to check the HTTPS certificate's trust, address and expiry. The app never bypasses certificate verification.")
            }
            Section {
                Button("Open iPhone Settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                }
            }
        }.navigationTitle("Connection help").navigationBarTitleDisplayMode(.inline)
    }
}
