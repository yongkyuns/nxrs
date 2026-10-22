import Foundation
import SwiftUI
import NxrsMonitorCore

@MainActor final class HomeSettings: ObservableObject {
    @Published var preferences = HomePreferences() { didSet { persist() } }
    private let defaults: UserDefaults
    private var scope = "unpaired"

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    var displayHomeName: String {
        let value = preferences.homeName.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? "My home" : value
    }

    func use(gateway: URL?) {
        // EndpointPolicy forbids userinfo/query/fragment; never put credentials in preferences.
        scope = gateway?.absoluteString ?? "unpaired"
        if let data = defaults.data(forKey: key), data.count <= 128 * 1024,
           let value = try? JSONDecoder().decode(HomePreferences.self, from: data) {
            preferences = value
        } else { preferences = HomePreferences() }
    }

    func update(_ camera: CameraSummary, name: String, room: String, favorite: Bool) {
        preferences.cameras[camera.cameraID.value] = CameraPersonalization(name: name, room: room, favorite: favorite)
    }
    func toggleFavorite(_ camera: CameraSummary) {
        var value = preferences.personalization(for: camera)
        value.favorite.toggle()
        preferences.cameras[camera.cameraID.value] = value
    }
    var colorScheme: ColorScheme? {
        switch preferences.appearance { case .system: return nil; case .light: return .light; case .dark: return .dark }
    }
    private var key: String { "org.nxrs.home.preferences.v1." + scope }
    private func persist() {
        guard let data = try? JSONEncoder().encode(preferences), data.count <= 128 * 1024 else { return }
        defaults.set(data, forKey: key)
    }
}

@MainActor final class HomeLibrary: ObservableObject {
    @Published private(set) var captures: [SavedCapture] = []
    @Published private(set) var urls: [UUID: URL] = [:]
    @Published var errorMessage: String?
    let repository: CaptureLibrary

    init(root: URL? = nil) {
        let base = root ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("NxrsHome/Captures", isDirectory: true)
        repository = CaptureLibrary(root: base)
    }
    func reload() async {
        do {
            let values = try await repository.captures()
            var files: [UUID: URL] = [:]
            for capture in values { files[capture.id] = try await repository.url(for: capture) }
            captures = values; urls = files; errorMessage = nil
        } catch { errorMessage = "Your saved library could not be opened. Your files have been kept." }
    }
    func favorite(_ capture: SavedCapture) async {
        do { try await repository.setFavorite(capture.id, value: !capture.favorite); await reload() }
        catch { errorMessage = "This favorite could not be updated. Please try again." }
    }
    func remove(_ capture: SavedCapture) async {
        do { try await repository.remove(capture.id); await reload() }
        catch { errorMessage = "This recording could not be deleted. Please try again." }
    }
    var bytes: Int64 { Int64(captures.reduce(0) { $0 + $1.byteCount }) }
}
