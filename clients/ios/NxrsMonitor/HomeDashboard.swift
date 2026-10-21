import SwiftUI
import NxrsMonitorCore

struct HomeDashboard: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject var settings: HomeSettings
    @State private var filter = "all"
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
        model.cameras.filter {
            filter == "all" || (filter == "favorites" && settings.preferences.personalization(for: $0).favorite)
                || filter == "room:" + settings.preferences.personalization(for: $0).room
        }
    }
    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    header
                    if model.isPreview {
                        Label("Design preview · no camera connected", systemImage: "paintpalette")
                            .font(.caption).foregroundStyle(HomeTheme.secondary)
                            .accessibilityIdentifier("design-preview-banner")
                    }
                    introduction
                    if let camera = model.selected {
                        featured(camera)
                        CameraCaptureControls(model: model)
                    } else if model.busy {
                        VStack(spacing: 12) { ProgressView(); Text("Finding your cameras…").font(.subheadline) }
                            .frame(maxWidth: .infinity).padding(48)
                    }
                    if let error = model.errorMessage {
                        ConnectionNotice(message: error) { model.reconnect() }
                    }
                    cameraSection
                    privacyNote
                }.padding(.horizontal, 22).padding(.top, 14).padding(.bottom, 28)
            }
            .background(HomeTheme.canvas)
            .toolbar(.hidden, for: .navigationBar)
            .refreshable { model.reload() }
            .onChange(of: rooms) { values in
                if filter.hasPrefix("room:"), !values.contains(String(filter.dropFirst(5))) {
                    filter = "all"
                }
            }
            .fullScreenCover(isPresented: $fullscreen) { FullScreenCameraView(model: model, settings: settings) }
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

    private var header: some View {
        HStack {
            HomeWordmark()
            Spacer()
            HomeIconButton(symbol: "plus", label: "Connect a home") { showConnection = true }
        }
    }

    private var introduction: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(greeting).font(.subheadline).foregroundStyle(HomeTheme.secondary)
            Button {
                draftHomeName = settings.preferences.homeName; showHomeName = true
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(settings.displayHomeName).font(.system(.largeTitle, design: .rounded).weight(.bold))
                        .multilineTextAlignment(.leading)
                    Image(systemName: "chevron.down").font(.caption.weight(.bold)).foregroundStyle(HomeTheme.secondary)
                }.foregroundStyle(HomeTheme.ink)
            }.buttonStyle(.plain).accessibilityLabel("Home name, \(settings.displayHomeName)")
            HStack(spacing: 7) {
                Image(systemName: model.live ? "checkmark.circle.fill" : "network")
                Text(model.isPreview ? "A little closer to home" : model.connectionSummary)
            }.font(.caption.weight(.medium)).foregroundStyle(HomeTheme.secondary)
        }
    }

    private func featured(_ camera: CameraSummary) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CameraLiveCanvas(model: model, interactive: false)
                HStack(alignment: .top) {
                    PlaybackBadge(status: model.status, live: model.live)
                    Spacer()
                    Button { settings.toggleFavorite(camera) } label: {
                        Image(systemName: settings.preferences.personalization(for: camera).favorite ? "star.fill" : "star")
                            .font(.system(size: 18))
                            .frame(width: 44, height: 44).background(.black.opacity(0.35), in: Circle())
                    }.buttonStyle(.plain).foregroundStyle(.white)
                        .accessibilityLabel(settings.preferences.personalization(for: camera).favorite ? "Remove camera favorite" : "Favorite camera")
                }.padding(12)
            }
            .aspectRatio(4 / 3, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
            (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(spacing: 12))) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(settings.preferences.displayName(for: camera)).font(.headline).foregroundStyle(HomeTheme.ink)
                    let room = settings.preferences.personalization(for: camera).room
                    Text(room.isEmpty ? (camera.source == .synthetic ? "Synthetic test source" : "Your camera") : room)
                        .font(.caption).foregroundStyle(HomeTheme.secondary)
                }
                HStack {
                    if typeSize.isAccessibilitySize { Spacer() }
                    Button { editingCamera = CameraEditSelection(camera: camera) } label: {
                        Image(systemName: "ellipsis").font(.system(size: 18)).frame(width: 44, height: 44)
                    }.buttonStyle(.plain).accessibilityLabel("Camera settings")
                    Button { fullscreen = true } label: {
                        Image(systemName: "arrow.up.left.and.arrow.down.right").font(.system(size: 18, weight: .semibold))
                            .frame(width: 44, height: 44).background(HomeTheme.canvas, in: Circle())
                    }.buttonStyle(.plain).accessibilityLabel("Full screen").accessibilityIdentifier("expand-camera")
                }.frame(maxWidth: .infinity, alignment: .trailing)
            }.foregroundStyle(HomeTheme.ink).padding(.horizontal, 16).padding(.vertical, 12)
        }.background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 28).strokeBorder(HomeTheme.line.opacity(0.7), lineWidth: 1))
    }

    private var cameraSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            HomeSectionHeading(title: "Your cameras", detail: "\(model.cameras.count) in this home")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    filterChip("All cameras", id: "all", symbol: nil)
                    filterChip("Favorites", id: "favorites", symbol: "star.fill")
                    ForEach(rooms, id: \.self) { room in filterChip(room, id: "room:" + room, symbol: nil) }
                }.padding(.vertical, 2)
            }.accessibilityIdentifier("camera-filters")
            if filtered.isEmpty {
                HomeEmptyState(symbol: filter == "favorites" ? "star" : "video.badge.plus",
                    title: model.cameras.isEmpty ? "Your cameras belong here" : "Nothing here just yet",
                    message: model.cameras.isEmpty ? "Cameras shared by your home will appear here. Pull down to refresh."
                        : "Favorite a camera or give it a room to make it easier to find.")
                    .background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 22))
            } else {
                LazyVStack(spacing: 10) {
                    ForEach(filtered, id: \.cameraID) { camera in
                        CameraListRow(camera: camera, name: settings.preferences.displayName(for: camera),
                            room: settings.preferences.personalization(for: camera).room,
                            selected: model.selected?.cameraID == camera.cameraID,
                            live: model.selected?.cameraID == camera.cameraID && model.live,
                            favorite: settings.preferences.personalization(for: camera).favorite,
                            action: { model.select(camera) },
                            edit: { editingCamera = CameraEditSelection(camera: camera) })
                    }
                }
            }
        }
    }

    private func filterChip(_ title: String, id: String, symbol: String?) -> some View {
        Button { filter = id } label: {
            HStack(spacing: 6) { if let symbol { Image(systemName: symbol) }; Text(title) }
                .font(.caption.weight(.semibold)).padding(.horizontal, 16).frame(minHeight: 44)
                .foregroundStyle(filter == id ? HomeTheme.darkForest : HomeTheme.secondary)
                .background(filter == id ? HomeTheme.lime : HomeTheme.surface, in: Capsule())
        }.buttonStyle(.plain).accessibilityAddTraits(filter == id ? .isSelected : [])
    }

    private var privacyNote: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "lock.shield").font(.title3)
            VStack(alignment: .leading, spacing: 4) {
                Text("Your home. Your moments.").font(.subheadline.weight(.semibold))
                Text("Captures stay on this iPhone until you choose to share them.")
                    .font(.caption).fixedSize(horizontal: false, vertical: true)
            }
        }.foregroundStyle(HomeTheme.secondary).padding(.top, 2)
    }
}

private struct CameraListRow: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    let camera: CameraSummary
    let name: String
    let room: String
    let selected: Bool
    let live: Bool
    let favorite: Bool
    let action: () -> Void
    let edit: () -> Void
    var body: some View {
        (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(spacing: 12))) {
            Button(action: action) {
                HStack(spacing: 12) {
                    if !typeSize.isAccessibilitySize { ZStack {
                        RoundedRectangle(cornerRadius: 17).fill(selected ? HomeTheme.lime.opacity(0.5) : HomeTheme.canvas)
                        Image(systemName: camera.source == .synthetic ? "testtube.2" : "video")
                            .font(.title3).foregroundStyle(HomeTheme.forest)
                    }.frame(width: 58, height: 58) }
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(name).font(.subheadline.weight(.semibold)).foregroundStyle(HomeTheme.ink)
                            if favorite { Image(systemName: "star.fill").font(.system(size: 13)).foregroundStyle(HomeTheme.forest) }
                        }
                        Text(live ? "Live now" : camera.source == .synthetic ? "Synthetic test source"
                             : camera.liveVideo ? (room.isEmpty ? "Tap to view" : room) : "Live view unavailable")
                            .font(.caption).foregroundStyle(HomeTheme.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if !typeSize.isAccessibilitySize {
                        Image(systemName: live ? "waveform" : "play.circle").foregroundStyle(HomeTheme.forest)
                    }
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(!camera.liveVideo)
                .accessibilityLabel("\(name), \(live ? "Live" : camera.liveVideo ? "Tap to view" : "Live view unavailable")")
            Button(action: edit) {
                if typeSize.isAccessibilitySize { Label("Edit camera", systemImage: "ellipsis").font(.subheadline).frame(minHeight: 48) }
                else { Image(systemName: "ellipsis").frame(width: 44, height: 48) }
            }
                .buttonStyle(.plain).accessibilityLabel("Edit \(name)")
        }.padding(12).foregroundStyle(HomeTheme.ink)
            .background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(selected ? HomeTheme.forest.opacity(0.4) : HomeTheme.line, lineWidth: 1))
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
