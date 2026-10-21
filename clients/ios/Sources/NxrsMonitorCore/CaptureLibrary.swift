import Foundation

/// A bounded, device-local library. Files are private and excluded from cloud backup.
/// Save/metadata changes are serialized; a failed index write never claims a saved capture.
public actor CaptureLibrary {
    private let root: URL
    private let maximumBytes: Int
    private let maximumItems: Int
    private var loaded: [SavedCapture]?
    private let fileManager = FileManager.default

    public init(root: URL, maximumBytes: Int = 512 * 1024 * 1024, maximumItems: Int = 1_000) {
        self.root = root
        self.maximumBytes = max(1, maximumBytes)
        self.maximumItems = max(1, maximumItems)
    }

    public func captures() throws -> [SavedCapture] { try readIndex() }
    public func url(for capture: SavedCapture) throws -> URL {
        guard let stored = try readIndex().first(where: { $0.id == capture.id }) else {
            throw CaptureLibraryError.missingCapture
        }
        return root.appendingPathComponent(stored.filename)
    }

    public func savePhoto(_ data: Data, cameraID: String, cameraName: String,
                          date: Date = Date()) throws -> SavedCapture {
        guard data.count >= 4, data.count <= 16 * 1024 * 1024,
              data.starts(with: [0xff, 0xd8]), data.suffix(2) == Data([0xff, 0xd9]) else {
            throw CaptureLibraryError.invalidMedia
        }
        let previous = try readIndex()
        try admit(bytes: data.count, previous: previous)
        let capture = SavedCapture(id: UUID(), cameraID: String(cameraID.prefix(64)),
            cameraName: String(cameraName.prefix(48)), createdAt: date, kind: .photo,
            duration: 0, byteCount: data.count, favorite: false)
        let destination = root.appendingPathComponent(capture.filename)
        try privateWrite(data, to: destination)
        do { try writeIndex([capture] + previous) }
        catch { try? fileManager.removeItem(at: destination); throw error }
        return capture
    }

    public func reserveClip() throws -> URL {
        let previous = try readIndex()
        try admit(bytes: 1, previous: previous)
        let staging = root.appendingPathComponent("Staging", isDirectory: true)
        try prepare(staging)
        return staging.appendingPathComponent(UUID().uuidString + ".mov")
    }

    public func finishClip(at staging: URL, duration: TimeInterval,
                           cameraID: String, cameraName: String,
                           date: Date = Date()) throws -> SavedCapture {
        let stagingRoot = root.appendingPathComponent("Staging", isDirectory: true).standardizedFileURL
        guard staging.deletingLastPathComponent().standardizedFileURL == stagingRoot,
              UUID(uuidString: staging.deletingPathExtension().lastPathComponent) != nil,
              staging.pathExtension == "mov",
              duration.isFinite, duration > 0, duration <= 61 else {
            throw CaptureLibraryError.invalidStagingFile
        }
        let attributes = try fileManager.attributesOfItem(atPath: staging.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue > 0,
              size.intValue <= 32 * 1024 * 1024 else { throw CaptureLibraryError.invalidMedia }
        let previous = try readIndex()
        try admit(bytes: size.intValue, previous: previous)
        let capture = SavedCapture(id: UUID(), cameraID: String(cameraID.prefix(64)),
            cameraName: String(cameraName.prefix(48)), createdAt: date, kind: .clip,
            duration: duration, byteCount: size.intValue, favorite: false)
        let destination = root.appendingPathComponent(capture.filename)
        try fileManager.moveItem(at: staging, to: destination)
        do {
            try protect(destination)
            try writeIndex([capture] + previous)
        } catch { try? fileManager.removeItem(at: destination); throw error }
        return capture
    }

    public func discardClip(at staging: URL) throws {
        guard staging.deletingLastPathComponent().standardizedFileURL ==
                root.appendingPathComponent("Staging", isDirectory: true).standardizedFileURL,
              UUID(uuidString: staging.deletingPathExtension().lastPathComponent) != nil,
              staging.pathExtension == "mov" else { throw CaptureLibraryError.invalidStagingFile }
        if fileManager.fileExists(atPath: staging.path) { try fileManager.removeItem(at: staging) }
    }

    public func setFavorite(_ id: UUID, value: Bool) throws {
        var previous = try readIndex()
        guard let index = previous.firstIndex(where: { $0.id == id }) else {
            throw CaptureLibraryError.missingCapture
        }
        previous[index].favorite = value
        try writeIndex(previous)
    }

    public func remove(_ id: UUID) throws {
        let previous = try readIndex()
        guard let capture = previous.first(where: { $0.id == id }) else {
            throw CaptureLibraryError.missingCapture
        }
        // Remove the index reference first. Never leave a visible item with a missing file.
        try writeIndex(previous.filter { $0.id != id })
        let file = root.appendingPathComponent(capture.filename)
        if fileManager.fileExists(atPath: file.path) { try fileManager.removeItem(at: file) }
    }

    private func admit(bytes: Int, previous: [SavedCapture]) throws {
        guard previous.count < maximumItems, bytes <= maximumBytes,
              previous.reduce(0, { $0 + $1.byteCount }) <= maximumBytes - bytes else {
            throw CaptureLibraryError.full
        }
    }

    private func readIndex() throws -> [SavedCapture] {
        if let loaded { return loaded }
        try prepare(root)
        let index = root.appendingPathComponent("library.json")
        guard fileManager.fileExists(atPath: index.path) else {
            try recoverUncommittedFiles(keeping: [])
            loaded = []; return []
        }
        let size = try fileManager.attributesOfItem(atPath: index.path)[.size] as? NSNumber
        guard let size, size.intValue <= 2 * 1024 * 1024 else { throw CaptureLibraryError.corruptIndex }
        let entries: [SavedCapture]
        do { entries = try JSONDecoder().decode([SavedCapture].self, from: Data(contentsOf: index)) }
        catch { throw CaptureLibraryError.corruptIndex }
        guard entries.count <= maximumItems, Set(entries.map(\.id)).count == entries.count,
              entries.allSatisfy({ $0.byteCount > 0 && $0.byteCount <= 32 * 1024 * 1024
                  && $0.duration.isFinite && $0.duration >= 0 && $0.duration <= 61 }) else {
            throw CaptureLibraryError.corruptIndex
        }
        // Only recover after validating the index. Corruption must preserve the user's files.
        try recoverUncommittedFiles(keeping: Set(entries.map(\.filename)))
        loaded = entries.sorted { $0.createdAt > $1.createdAt }
        return loaded ?? []
    }

    private func recoverUncommittedFiles(keeping filenames: Set<String>) throws {
        for file in try fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) {
            let ownedMedia = ["jpg", "mov"].contains(file.pathExtension)
                && UUID(uuidString: file.deletingPathExtension().lastPathComponent) != nil
            if ownedMedia && !filenames.contains(file.lastPathComponent) {
                try fileManager.removeItem(at: file)
            }
        }
        let staging = root.appendingPathComponent("Staging", isDirectory: true)
        if fileManager.fileExists(atPath: staging.path) {
            for file in try fileManager.contentsOfDirectory(at: staging, includingPropertiesForKeys: nil) {
                if file.pathExtension == "mov", UUID(uuidString: file.deletingPathExtension().lastPathComponent) != nil {
                    try fileManager.removeItem(at: file)
                }
            }
        }
    }

    private func writeIndex(_ entries: [SavedCapture]) throws {
        try privateWrite(JSONEncoder().encode(entries), to: root.appendingPathComponent("library.json"))
        loaded = entries
    }

    private func prepare(_ directory: URL) throws {
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true,
                                        attributes: [.posixPermissions: 0o700])
        try protect(directory)
    }

    private func privateWrite(_ data: Data, to url: URL) throws {
        #if os(iOS)
        try data.write(to: url, options: [.atomic, .completeFileProtection])
        #else
        try data.write(to: url, options: .atomic)
        #endif
        try protect(url)
    }

    private func protect(_ source: URL) throws {
        var url = source
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try url.setResourceValues(values)
        var attributes: [FileAttributeKey: Any] = [.posixPermissions: url.hasDirectoryPath ? 0o700 : 0o600]
        #if os(iOS)
        attributes[.protectionKey] = FileProtectionType.complete
        #endif
        try fileManager.setAttributes(attributes, ofItemAtPath: url.path)
    }
}
