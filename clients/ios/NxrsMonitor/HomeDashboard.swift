import SwiftUI
import NxrsMonitorCore

struct HomeDashboard: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject var settings: HomeSettings
    @State private var filter = "all"
    // Presentation dismisses before playback demand is cleared in onDismiss.
    @State private var fullscreen = false
    @State private var editingCamera: CameraEditSelection?
    @State private var showConnection = false
    @State private var showHomeName = false
    @State private var draftHomeName = ""
    @Environment(\.dynamicTypeSize) private var typeSize

    private struct CameraEditSelection: Identifiable { let camera: CameraSummary; var id: ResourceID { camera.cameraID } }
    private var rooms: [String] {
        Array(Set(model.cameras.map { settings.preferences.personalization(for: $0).room }.filter { !$0.isEmpty })).sorted()
    }
    private var filtered: [CameraSummary] {
        if model.cameras.count < 2 { return model.cameras }
        return model.cameras.filter {
            filter == "all" || (filter == "favorites" && settings.preferences.personalization(for: $0).favorite)
                || filter == "room:" + settings.preferences.personalization(for: $0).room
        }
    }
    private var columns: [GridItem] {
        typeSize.isAccessibilitySize ? [GridItem(.flexible())] : [GridItem(.adaptive(minimum: 280), spacing: 16)]
    }

    var body: some View {
        GeometryReader { geometry in
            dashboard(compactHeight: geometry.size.width > geometry.size.height)
        }
    }

    private func dashboard(compactHeight: Bool) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: compactHeight ? 10 : 16) {
                    header(compactHeight: compactHeight)
                    if model.isPreview && !compactHeight {
                        Label("Preview · no live video", systemImage: "paintpalette")
                            .font(.caption).foregroundStyle(HomeTheme.secondary)
                            .accessibilityIdentifier("design-preview-banner")
                    }
                    if let error = model.errorMessage {
                        ConnectionNotice(message: error) { model.reconnect() }
                    }
                    if model.cameras.count > 1 && !compactHeight { filters }
                    if model.busy && model.cameras.isEmpty {
                        ProgressView("Loading cameras…").frame(maxWidth: .infinity).padding(48)
                    } else if filtered.isEmpty {
                        HomeEmptyState(symbol: filter == "favorites" ? "star" : "video.badge.plus",
                            title: model.cameras.isEmpty ? "No cameras yet" : "Nothing here just yet",
                            message: model.cameras.isEmpty ? "Cameras shared with your home appear here." : "Try another filter.")
                    } else {
                        LazyVGrid(columns: columns, spacing: 16) {
                            ForEach(filtered, id: \.cameraID) { camera in
                                if let session = model.sessions[camera.cameraID] {
                                    CameraCard(camera: camera, session: session, settings: settings, isPreview: model.isPreview,
                                        compact: filtered.count > 1, showPlayback: model.cameras.count > 1,
                                        resume: { model.resume(camera) }, pause: { model.pause(camera) },
                                        expand: { model.setFullscreen(camera); fullscreen = true },
                                        edit: { editingCamera = CameraEditSelection(camera: camera) })
                                        .onAppear { model.setVisible(camera, visible: true) }
                                        .onDisappear { model.setVisible(camera, visible: false) }
                                }
                            }
                        }.accessibilityIdentifier("camera-grid")
                        if model.cameras.count == 1 { CameraCaptureControls(model: model) }
                    }
                }.padding(.horizontal, 20).padding(.top, 12).padding(.bottom, 20)
            }
            .background(HomeTheme.canvas)
            .toolbar(.hidden, for: .navigationBar)
            .refreshable { model.reload() }
            .onChange(of: rooms) { values in
                if filter.hasPrefix("room:"), !values.contains(String(filter.dropFirst(5))) { filter = "all" }
            }
            .onChange(of: model.cameras.count) { count in
                if count < 2 { filter = "all" }
            }
            .onChange(of: model.fullscreen) { id in
                if id == nil { fullscreen = false }
            }
            .fullScreenCover(isPresented: $fullscreen, onDismiss: { model.setFullscreen(nil) }) { FullScreenCameraView(model: model, settings: settings) }
            .sheet(item: $editingCamera) { item in CameraEditView(camera: item.camera, settings: settings) }
            .sheet(isPresented: $showConnection) { HomeSetupView(model: model, replacing: true) }
            .alert("Name your home", isPresented: $showHomeName) {
                TextField("Home name", text: $draftHomeName)
                Button("Save") {
                    let name = draftHomeName.trimmingCharacters(in: .whitespacesAndNewlines)
                    settings.preferences.homeName = name.isEmpty ? "My home" : String(name.prefix(48))
                }
                Button("Cancel", role: .cancel) {}
            }
        }
    }

    private func header(compactHeight: Bool) -> some View {
        HStack {
            Button {
                draftHomeName = settings.preferences.homeName; showHomeName = true
            } label: {
                Text(settings.displayHomeName).font(.title2.weight(.semibold))
                    .multilineTextAlignment(.leading).foregroundStyle(HomeTheme.ink)
            }.buttonStyle(.plain).accessibilityLabel("Home name, \(settings.displayHomeName)")
            Spacer()
            if compactHeight && model.isPreview {
                Text("Preview · no live video").font(.caption).foregroundStyle(HomeTheme.secondary)
                    .accessibilityIdentifier("design-preview-banner")
            }
            if compactHeight && model.cameras.count > 1 {
                Menu {
                    Picker("Cameras", selection: $filter) {
                        Text("All cameras").tag("all")
                        Text("Favorites").tag("favorites")
                        ForEach(rooms, id: \.self) { Text($0).tag("room:" + $0) }
                    }
                } label: {
                    Image(systemName: "line.3.horizontal.decrease").frame(width: 44, height: 44)
                }.accessibilityLabel("Filter cameras").foregroundStyle(HomeTheme.ink)
            }
            HomeIconButton(symbol: "plus", label: "Connect a home") { showConnection = true }
        }
    }

    private var filters: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                filterChip("All cameras", id: "all", symbol: nil)
                filterChip("Favorites", id: "favorites", symbol: "star.fill")
                ForEach(rooms, id: \.self) { room in filterChip(room, id: "room:" + room, symbol: nil) }
            }
        }.accessibilityIdentifier("camera-filters")
    }

    private func filterChip(_ title: String, id: String, symbol: String?) -> some View {
        Button { filter = id } label: {
            HStack(spacing: 6) { if let symbol { Image(systemName: symbol) }; Text(title) }
                .font(.caption.weight(.semibold)).padding(.horizontal, 16).frame(minHeight: 44)
                .foregroundStyle(filter == id ? HomeTheme.darkForest : HomeTheme.secondary)
                .background(filter == id ? HomeTheme.lime : HomeTheme.surface, in: Capsule())
        }.buttonStyle(.plain).accessibilityAddTraits(filter == id ? .isSelected : [])
    }
}

struct ConnectionNotice: View {
    let message: String
    let retry: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Let's get you reconnected", systemImage: "wifi.exclamationmark").font(.subheadline.weight(.semibold))
            Text(message).font(.caption).fixedSize(horizontal: false, vertical: true)
            Button("Try again", action: retry).font(.subheadline.weight(.semibold))
                .frame(minHeight: 44)
        }.foregroundStyle(HomeTheme.warning).padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(HomeTheme.warning.opacity(0.08), in: RoundedRectangle(cornerRadius: 20))
    }
}

private struct CameraCard: View {
    let camera: CameraSummary
    @ObservedObject var session: CameraSession
    @ObservedObject var settings: HomeSettings
    let isPreview: Bool
    let compact: Bool
    let showPlayback: Bool
    let resume, pause, expand, edit: () -> Void
    @Environment(\.dynamicTypeSize) private var typeSize
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CameraLiveCanvas(session: session, isPreview: isPreview, resume: resume, interactive: false)
                HStack(alignment: .top) {
                    if session.live { PlaybackBadge(status: session.status, live: true) }
                    Spacer()
                    Button { settings.toggleFavorite(camera) } label: {
                        Image(systemName: settings.preferences.personalization(for: camera).favorite ? "star.fill" : "star")
                            .font(.system(size: 18))
                            .frame(width: 44, height: 44).background(.black.opacity(0.35), in: Circle())
                    }.buttonStyle(.plain).foregroundStyle(.white)
                        .accessibilityLabel(settings.preferences.personalization(for: camera).favorite ? "Remove camera favorite" : "Favorite camera")
                }.padding(12)
            }
            .aspectRatio(compact ? 16.0 / 9.0 : 4.0 / 3.0, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
            (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(spacing: 8))) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(settings.preferences.displayName(for: camera)).font(.headline).foregroundStyle(HomeTheme.ink)
                    let room = settings.preferences.personalization(for: camera).room
                    if !compact && !room.isEmpty { Text(room).font(.caption).foregroundStyle(HomeTheme.secondary) }
                    CameraDetectionStatus(session: session)
                    if session.isRecording {
                        Label("Recording", systemImage: "record.circle.fill").font(.caption).foregroundStyle(.red)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
                HStack(spacing: 0) {
                    if showPlayback {
                        Button(action: session.active ? pause : resume) {
                            Image(systemName: session.active ? "pause.fill" : "play.fill").frame(width: 44, height: 44)
                        }.disabled(session.retiring || !camera.liveVideo)
                            .accessibilityLabel(session.active ? "Pause" : "Watch")
                            .accessibilityIdentifier("watch-" + camera.cameraID.value)
                    }
                    Button(action: edit) {
                        Image(systemName: "ellipsis").frame(width: 44, height: 44)
                    }.accessibilityLabel("Edit \(settings.preferences.displayName(for: camera))")
                    Button(action: expand) {
                        Image(systemName: "arrow.up.left.and.arrow.down.right").frame(width: 44, height: 44)
                    }.accessibilityLabel("Full screen").accessibilityIdentifier("expand-camera")
                }.buttonStyle(.plain).font(.system(size: 17, weight: .medium))
            }.foregroundStyle(HomeTheme.ink).padding(.horizontal, 14).padding(.vertical, 10)
        }.background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 24).strokeBorder(HomeTheme.line.opacity(0.7), lineWidth: 1))
    }
}
