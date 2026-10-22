import SwiftUI
import AVKit
import NxrsMonitorCore

@MainActor private final class HomeActivity: ObservableObject {
    @Published private(set) var page: ActivityPage?
    @Published private(set) var events: [ActivityEvent] = []
    @Published private(set) var loading = false
    @Published private(set) var unavailable = false
    @Published private(set) var checkedAt: Date?
    @Published private(set) var nextCursor: String?
    @Published var errorMessage: String?
    @Published var playingURL: URL?
    @Published private(set) var downloading: String?
    @Published private(set) var updating: String?
    private var scope = UUID()
    private var requests = 0
    private var preview = false
    private let thumbnails = NSCache<NSString, UIImage>()
    private let cache = FileManager.default.temporaryDirectory.appendingPathComponent("NxrsActivity-" + UUID().uuidString, isDirectory: true)

    init() { thumbnails.countLimit = 40; thumbnails.totalCostLimit = 12 * 1024 * 1024 }
    deinit { try? FileManager.default.removeItem(at: cache) }

    func clear() {
        scope = UUID(); page = nil; events = []; loading = false; playingURL = nil; downloading = nil; checkedAt = nil; nextCursor = nil
        updating = nil; thumbnails.removeAllObjects(); try? FileManager.default.removeItem(at: cache)
    }
    func load(model: MonitorModel, camera: ResourceID, more: Bool = false, reset: Bool = true, quiet: Bool = false) async {
        if !more && reset { clear(); unavailable = false; errorMessage = nil }
        guard !loading else { return }
        let ticket = scope; loading = true; preview = model.isPreview
        defer { if scope == ticket { loading = false } }
        do {
            let value: ActivityPage
            #if DEBUG
            if preview { value = try previewPage(camera: camera) }
            else {
                let (client, credential) = try model.activityConnection()
                value = try await client.page(camera: camera, before: more ? nextCursor : nil, credential: credential)
            }
            #else
            let (client, credential) = try model.activityConnection()
            value = try await client.page(camera: camera, before: more ? nextCursor : nil, credential: credential)
            #endif
            try Task.checkCancellation(); guard ticket == scope else { return }
            page = value; checkedAt = Date()
            if more {
                let ids = Set(events.map(\.id)); events += value.events.filter { !ids.contains($0.id) }
            } else if !reset {
                let ids = Set(value.events.map(\.id))
                let oldest = value.events.last?.startedAtMs ?? UInt64.max
                let cutoff = value.observedAtMs > value.retentionSeconds * 1000 ? value.observedAtMs - value.retentionSeconds * 1000 : 0
                let older = events.filter { !ids.contains($0.id) && $0.startedAtMs < oldest && ($0.kept || $0.endedAtMs >= cutoff) }
                events = Array((value.events + older).prefix(500))
            } else { events = value.events }
            if more || reset || events.count <= 50 { nextCursor = value.nextCursor }
        } catch {
            guard !Task.isCancelled, ticket == scope else { return }
            if error as? MonitorError == .notFound { unavailable = true }
            else if error as? MonitorError == .unauthenticated || error as? MonitorError == .forbidden {
                clear(); errorMessage = message(error)
            } else if !quiet { errorMessage = message(error) }
        }
    }
    func thumbnail(_ event: ActivityEvent, model: MonitorModel) async -> UIImage? {
        if let image = thumbnails.object(forKey: event.id as NSString) { return image }
        if preview { return nil }
        let ticket = scope
        do {
            // View tasks are cancellation-aware; at most three media requests are admitted.
            while requests >= 3 { try await Task.sleep(nanoseconds: 50_000_000); guard scope == ticket else { return nil } }
            try Task.checkCancellation(); requests += 1
            defer { requests -= 1 }
            let (client, credential) = try model.activityConnection()
            let bytes = try await client.media(event: event, thumbnail: true, credential: credential)
            try Task.checkCancellation(); guard scope == ticket, let image = UIImage(data: bytes) else { return nil }
            thumbnails.setObject(image, forKey: event.id as NSString, cost: Int(image.size.width * image.size.height * 4))
            return image
        } catch { return nil }
    }
    func play(_ event: ActivityEvent, model: MonitorModel) async {
        guard downloading == nil else { return }
        if preview { errorMessage = "Design preview uses sample activity. Connect your home to play real clips."; return }
        let ticket = scope; downloading = event.id
        defer { if scope == ticket { downloading = nil } }
        do {
            let (client, credential) = try model.activityConnection()
            let bytes = try await client.media(event: event, thumbnail: false, credential: credential)
            try Task.checkCancellation(); guard scope == ticket else { return }
            try? FileManager.default.removeItem(at: cache)
            try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true,
                attributes: [.protectionKey: FileProtectionType.complete])
            var directory = cache; var values = URLResourceValues(); values.isExcludedFromBackup = true
            try directory.setResourceValues(values)
            let url = cache.appendingPathComponent(event.id + ".mp4")
            try bytes.write(to: url, options: [.atomic, .completeFileProtection]); playingURL = url
        } catch { if !Task.isCancelled, scope == ticket { errorMessage = message(error) } }
    }
    func keep(_ event: ActivityEvent, model: MonitorModel) async {
        guard updating == nil else { return }
        if preview { errorMessage = "Connect your home to keep a real activity clip."; return }
        let ticket = scope; updating = event.id
        defer { if scope == ticket { updating = nil } }
        do {
            let (client, credential) = try model.activityConnection()
            let updated = try await client.keep(event: event, kept: !event.kept, credential: credential)
            try Task.checkCancellation(); guard scope == ticket else { return }
            if let index = events.firstIndex(where: { $0.id == event.id }) { events[index] = updated }
        } catch { if !Task.isCancelled, scope == ticket { errorMessage = message(error) } }
    }
    func remove(_ event: ActivityEvent, model: MonitorModel) async {
        guard updating == nil else { return }
        if preview { errorMessage = "Connect your home to manage real activity clips."; return }
        let ticket = scope; updating = event.id
        defer { if scope == ticket { updating = nil } }
        do {
            let (client, credential) = try model.activityConnection()
            try await client.remove(event: event, credential: credential)
            try Task.checkCancellation(); guard scope == ticket else { return }
            events.removeAll { $0.id == event.id }; thumbnails.removeObject(forKey: event.id as NSString)
        } catch { if !Task.isCancelled, scope == ticket { errorMessage = message(error) } }
    }
    private func message(_ error: Error) -> String {
        switch error as? MonitorError {
        case .unauthenticated, .forbidden: return "Your home could not authorize this request. Check your connection in Settings."
        case .notFound: return "This clip is no longer available. Refresh Activity to see current recordings."
        case .rateLimited: return "Your home is busy saving footage. Try again shortly."
        default: return "Activity could not be loaded. Your saved recordings are still on this iPhone. Try again when your home is reachable."
        }
    }
    #if DEBUG
    private func previewPage(camera: ResourceID) throws -> ActivityPage {
        let now = UInt64(Date().timeIntervalSince1970 * 1000)
        var samples: [[String: Any]] = []
        for i in 0..<3 {
            let start = now - UInt64(120_000 + i * 3_600_000)
            samples.append(["event_id": String(format: "%032x", i + 1), "camera_id": camera.value,
                "kind": "motion", "started_at_ms": start, "ended_at_ms": start + 12_000,
                "clip_bytes": 1024, "thumbnail_bytes": 1024, "kept": i == 1, "interrupted": false])
        }
        let payload: [String: Any] = ["api_version":"1", "camera_id":camera.value,
            "events": samples, "next_cursor":NSNull(), "can_manage":true,
            "used_bytes":6144, "max_bytes":1_073_741_824, "retention_seconds":259200,
            "health":NSNull(), "observed_at_ms":now]
        let data = try JSONSerialization.data(withJSONObject: payload)
        return try JSONDecoder().decode(ActivityPage.self, from: data)
    }
    #endif
}

struct HomeActivityView: View {
    @ObservedObject var model: MonitorModel
    @ObservedObject private var settings: HomeSettings
    @StateObject private var activity = HomeActivity()
    @State private var cameraID = ""
    @State private var keptOnly = false
    @State private var mediaTask: Task<Void, Never>?
    @State private var deleting: ActivityEvent?
    @Environment(\.scenePhase) private var scenePhase
    private var camera: CameraSummary? { model.cameras.first { $0.cameraID.value == cameraID } ?? model.selected ?? model.cameras.first }
    private var scopeID: String { "\(model.enrollmentID):\(camera?.cameraID.value ?? ""): \(scenePhase == .active)" }
    private var visible: [ActivityEvent] { activity.events.filter { !keptOnly || $0.kept } }
    private var days: [Date] { Array(Set(visible.map { Calendar.current.startOfDay(for: $0.startedAt) })).sorted(by: >) }
    init(model: MonitorModel) { self.model = model; self.settings = model.settings }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if model.isPreview {
                        Label("Design preview · sample activity", systemImage: "paintbrush")
                            .font(.caption.weight(.medium)).foregroundStyle(HomeTheme.warning)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("What happened at home.").font(.title3.weight(.semibold)).foregroundStyle(HomeTheme.ink)
                        Text("Motion clips recorded by your home, even when this app is closed.")
                            .font(.subheadline).foregroundStyle(HomeTheme.secondary)
                    }
                    if let camera {
                        Picker("Camera", selection: $cameraID) {
                            ForEach(model.cameras, id: \.cameraID) { item in Text(settings.preferences.displayName(for: item)).tag(item.cameraID.value) }
                        }.pickerStyle(.menu).frame(minHeight: 44).accessibilityIdentifier("activity-camera")
                            .onAppear { if cameraID.isEmpty { cameraID = camera.cameraID.value } }
                        if camera.source == .synthetic { Label("Test camera", systemImage: "testtube.2").font(.caption).foregroundStyle(HomeTheme.warning) }
                    }
                    if let page = activity.page {
                        TimelineView(.periodic(from: .now, by: 2)) { context in monitoring(page, at: context.date) }
                    }
                    HStack(spacing: 8) {
                        filter("All activity", active: !keptOnly) { keptOnly = false }
                        filter("Kept", active: keptOnly) { keptOnly = true }
                    }
                    if activity.loading && activity.events.isEmpty { ProgressView("Loading activity…").frame(maxWidth: .infinity).padding(30) }
                    else if activity.unavailable {
                        HomeEmptyState(symbol: "clock.badge.questionmark", title: "Activity isn’t available",
                            message: "Automatic recording may not be enabled for this camera. Check your home connection and camera access in Settings.")
                    } else if visible.isEmpty {
                        HomeEmptyState(symbol: keptOnly ? "bookmark" : "figure.walk", title: keptOnly ? "No kept clips" : "No activity yet",
                            message: keptOnly ? "Keep a motion clip to save it beyond automatic retention." : "Motion clips will appear here when your home records movement.")
                    } else {
                        ForEach(days, id: \.self) { day in
                            VStack(alignment: .leading, spacing: 12) {
                                Text(dayTitle(day)).font(.headline).foregroundStyle(HomeTheme.ink)
                                ForEach(visible.filter { Calendar.current.isDate($0.startedAt, inSameDayAs: day) }) { event in
                                    ActivityRow(event: event, name: camera.map { settings.preferences.displayName(for: $0) } ?? "Camera",
                                        activity: activity, model: model,
                                        play: { mediaTask?.cancel(); mediaTask = Task { await activity.play(event, model: model) } },
                                        keep: { mediaTask?.cancel(); mediaTask = Task { await activity.keep(event, model: model) } },
                                        remove: { deleting = event })
                                }
                            }
                        }
                    }
                    if activity.nextCursor != nil, activity.events.count < 500 {
                        Button(activity.loading ? "Loading…" : "Earlier activity") {
                            guard let camera else { return }; mediaTask?.cancel()
                            mediaTask = Task { await activity.load(model: model, camera: camera.cameraID, more: true) }
                        }.disabled(activity.loading).frame(minHeight: 44)
                    }
                }.padding(22).padding(.bottom, 16)
            }.background(HomeTheme.canvas).navigationTitle("Activity")
                .refreshable { if let camera { await activity.load(model: model, camera: camera.cameraID) } }
                .toolbar { ToolbarItem(placement: .primaryAction) {
                    Button { guard let camera else { return }; mediaTask?.cancel()
                        mediaTask = Task { await activity.load(model: model, camera: camera.cameraID) }
                    } label: { Label("Refresh activity", systemImage: "arrow.clockwise") }.disabled(activity.loading)
                } }
                .task(id: scopeID) {
                    mediaTask?.cancel()
                    guard scenePhase == .active, let camera else { activity.clear(); return }
                    await activity.load(model: model, camera: camera.cameraID)
                    while !Task.isCancelled {
                        do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
                        await activity.load(model: model, camera: camera.cameraID, reset: false, quiet: true)
                    }
                }
                .onDisappear { mediaTask?.cancel(); activity.clear() }
                .sheet(isPresented: Binding(get: { activity.playingURL != nil }, set: { if !$0 { activity.playingURL = nil } })) {
                    if let url = activity.playingURL { ActivityPlayback(url: url) }
                }
                .alert("Activity", isPresented: Binding(get: { activity.errorMessage != nil }, set: { if !$0 { activity.errorMessage = nil } })) {
                    Button("OK") { activity.errorMessage = nil }
                } message: { Text(activity.errorMessage ?? "") }
                .confirmationDialog("Delete this activity clip?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                    if let event = deleting {
                        Button("Delete clip", role: .destructive) {
                            mediaTask?.cancel(); mediaTask = Task { await activity.remove(event, model: model) }; deleting = nil
                        }
                    }
                } message: { Text("This deletes the recording from your home, including a kept clip. Recordings saved separately on this iPhone stay available.") }
        }.tint(HomeTheme.forest)
    }
    private func filter(_ title: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { Text(title).font(.caption.weight(.semibold)).padding(.horizontal, 20).frame(minHeight: 44)
            .foregroundStyle(active ? HomeTheme.darkForest : HomeTheme.secondary)
            .background(active ? HomeTheme.lime : HomeTheme.surface, in: Capsule())
        }.buttonStyle(.plain).accessibilityAddTraits(active ? .isSelected : [])
    }
    private func monitoring(_ page: ActivityPage, at date: Date) -> some View {
        let current = page.monitoring && date.timeIntervalSince(activity.checkedAt ?? .distantPast) < 15
        return VStack(alignment: .leading, spacing: 8) {
            Label(model.isPreview ? "Sample activity" : current ? "Watching for movement" : healthTitle(page),
                  systemImage: current && !model.isPreview ? "checkmark.circle.fill" : "info.circle")
                .font(.subheadline.weight(.semibold)).foregroundStyle(current ? HomeTheme.forest : HomeTheme.warning)
            Text("Automatic clips stay for \(Int(page.retentionSeconds / 86400)) days. Kept clips stay until you remove them.")
                .font(.caption).foregroundStyle(HomeTheme.secondary)
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 18))
    }
    private func healthTitle(_ page: ActivityPage) -> String {
        switch page.health?.state {
        case "storage_full": return "Storage is full · recording needs attention"
        case "recording_error": return "Recording needs attention"
        case "connecting", "reconnecting": return "Reconnecting to camera"
        default: return "Motion recording isn’t currently confirmed"
        }
    }
    private func dayTitle(_ day: Date) -> String {
        Calendar.current.isDateInToday(day) ? "Today" : Calendar.current.isDateInYesterday(day) ? "Yesterday" : day.formatted(.dateTime.month(.wide).day())
    }
}

private struct ActivityRow: View {
    let event: ActivityEvent
    let name: String
    @ObservedObject var activity: HomeActivity
    let model: MonitorModel
    let play: () -> Void
    let keep: () -> Void
    let remove: () -> Void
    @State private var image: UIImage?
    @Environment(\.dynamicTypeSize) private var typeSize
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button(action: play) {
                (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12)) : AnyLayout(HStackLayout(spacing: 14))) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 14).fill(HomeTheme.forest.opacity(0.08))
                        if let image { Image(uiImage: image).resizable().scaledToFill().frame(width: 104, height: 78).clipped() }
                        else { Image(systemName: "figure.walk").font(.title2).foregroundStyle(HomeTheme.secondary) }
                        if activity.downloading == event.id { ProgressView().padding(8).background(HomeTheme.surface, in: Circle()) }
                        else { Image(systemName: "play.fill").font(.caption).padding(8).background(.black.opacity(0.5), in: Circle()).foregroundStyle(.white) }
                    }.frame(width: 104, height: 78).clipShape(RoundedRectangle(cornerRadius: 14)).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Motion detected").font(.subheadline.weight(.semibold)).foregroundStyle(HomeTheme.ink)
                        Text(name).font(.caption).foregroundStyle(HomeTheme.secondary)
                        Text("\(event.startedAt.formatted(date: .omitted, time: .shortened)) · \(Int(event.duration.rounded())) sec")
                            .font(.caption).foregroundStyle(HomeTheme.secondary)
                        if event.interrupted { Text("Recording interrupted").font(.caption).foregroundStyle(HomeTheme.warning) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }.buttonStyle(.plain).disabled(activity.downloading != nil)
                .accessibilityLabel("Motion at \(name), \(event.startedAt.formatted()), \(Int(event.duration)) seconds. Play clip")
            if activity.page?.canManage == true {
                HStack {
                Button(action: keep) { Label(event.kept ? "Kept" : "Keep clip", systemImage: event.kept ? "bookmark.fill" : "bookmark")
                    .font(.caption.weight(.semibold)).frame(minHeight: 44)
                }.buttonStyle(.plain).foregroundStyle(HomeTheme.forest).disabled(activity.updating != nil)
                    .accessibilityLabel(event.kept ? "Remove Keep from clip" : "Keep clip beyond automatic retention")
                    Spacer()
                    Menu {
                        Button("Delete activity clip", systemImage: "trash", role: .destructive, action: remove)
                    } label: { Image(systemName: "ellipsis").frame(width: 44, height: 44) }
                        .accessibilityLabel("Activity clip options").disabled(activity.updating != nil)
                }
            } else if event.kept { Label("Kept", systemImage: "bookmark.fill").font(.caption).foregroundStyle(HomeTheme.forest) }
        }.padding(14).background(HomeTheme.surface, in: RoundedRectangle(cornerRadius: 20))
            .task(id: event.id) { image = await activity.thumbnail(event, model: model) }
    }
}

private struct ActivityPlayback: View {
    let url: URL
    @State private var player: AVPlayer?
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        NavigationStack {
            VideoPlayer(player: player).background(.black).navigationTitle("Activity clip").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .onAppear { player = AVPlayer(url: url); player?.play() }
                .onDisappear { player?.pause(); player = nil }
                .onChange(of: scenePhase) { if $0 != .active { player?.pause() } }
        }
    }
}
