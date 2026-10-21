import XCTest
@testable import NxrsMonitorCore

final class HomeExperienceTests: XCTestCase {
    func testInvitationRequiresTrustedOriginsVersionAndCredential() throws {
        let data = Data(#"{"version":1,"gateway":"https://home.example","viewerCredential":"private-viewer","homeName":" Our place "}"#.utf8)
        let invitation = try EnrollmentInvitation(data: data)
        XCTAssertEqual(invitation.homeName, "Our place")
        XCTAssertFalse(String(describing: invitation).contains("private-viewer"))
        XCTAssertFalse(String(reflecting: invitation).contains("private-viewer"))
        XCTAssertTrue(Mirror(reflecting: invitation).children.isEmpty)
        for text in [
            #"{"version":2,"gateway":"https://home.example","viewerCredential":"key"}"#,
            #"{"version":1,"gateway":"http://home.example","viewerCredential":"key"}"#,
            #"{"version":1,"gateway":"https://home.example","viewerCredential":""}"#,
            #"{"version":1,"gateway":"https://home.example","mediaOrigin":"http://video.example","viewerCredential":"key"}"#
        ] { XCTAssertThrowsError(try EnrollmentInvitation(data: Data(text.utf8))) }
        XCTAssertThrowsError(try EnrollmentInvitation(data: Data(repeating: 1, count: 16_385)))
    }

    func testPersonalizationHasBoundedTrimmedNames() {
        let value = CameraPersonalization(name: "  Living room  ", room: String(repeating: "a", count: 100))
        XCTAssertEqual(value.name, "Living room")
        XCTAssertEqual(value.room.count, 32)
        XCTAssertFalse(value.favorite)
    }
}

final class CaptureLibraryTests: XCTestCase {
    private let jpeg = Data([0xff, 0xd8, 0x10, 0x11, 0xff, 0xd9])

    private func directory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testPhotoFavoritePersistenceAndDeletion() async throws {
        let root = try directory()
        let library = CaptureLibrary(root: root)
        let capture = try await library.savePhoto(jpeg, cameraID: "cam_a", cameraName: "Front door")
        let url = try await library.url(for: capture)
        XCTAssertEqual(try Data(contentsOf: url), jpeg)
        try await library.setFavorite(capture.id, value: true)
        let reloaded = try await CaptureLibrary(root: root).captures()
        XCTAssertEqual(reloaded.count, 1)
        XCTAssertTrue(reloaded[0].favorite)
        try await library.remove(capture.id)
        let remaining = try await library.captures()
        XCTAssertTrue(remaining.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func testQuotaDoesNotDeleteExistingCapturesOrAcceptInvalidMedia() async throws {
        let library = CaptureLibrary(root: try directory(), maximumBytes: jpeg.count, maximumItems: 1)
        _ = try await library.savePhoto(jpeg, cameraID: "cam_a", cameraName: "Camera")
        do { _ = try await library.savePhoto(jpeg, cameraID: "cam_a", cameraName: "Camera"); XCTFail("quota") }
        catch { XCTAssertEqual(error as? CaptureLibraryError, .full) }
        let remaining = try await library.captures()
        XCTAssertEqual(remaining.count, 1)
        do { _ = try await library.savePhoto(Data([1, 2]), cameraID: "a", cameraName: "Camera"); XCTFail("media") }
        catch { XCTAssertEqual(error as? CaptureLibraryError, .invalidMedia) }
    }

    func testClipOnlyAdmitsOwnedStagingPathsAndBoundedDurations() async throws {
        let root = try directory()
        let library = CaptureLibrary(root: root)
        let staging = try await library.reserveClip()
        try Data(repeating: 5, count: 64).write(to: staging)
        do {
            _ = try await library.finishClip(at: staging, duration: 70, cameraID: "a", cameraName: "Camera")
            XCTFail("duration")
        } catch { XCTAssertEqual(error as? CaptureLibraryError, .invalidStagingFile) }
        let capture = try await library.finishClip(at: staging, duration: 1.5, cameraID: "a", cameraName: "Camera")
        XCTAssertEqual(capture.kind, .clip)
        XCTAssertFalse(FileManager.default.fileExists(atPath: staging.path))
        let unrelated = root.appendingPathComponent("unrelated.mov")
        try Data([1]).write(to: unrelated)
        do { try await library.discardClip(at: unrelated); XCTFail("path") }
        catch { XCTAssertEqual(error as? CaptureLibraryError, .invalidStagingFile) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: unrelated.path))
    }

    func testCorruptIndexIsNotSilentlyReplaced() async throws {
        let root = try directory()
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let index = root.appendingPathComponent("library.json")
        let invalid = Data("not a library".utf8)
        try invalid.write(to: index)
        do { _ = try await CaptureLibrary(root: root).captures(); XCTFail("corruption") }
        catch { XCTAssertEqual(error as? CaptureLibraryError, .corruptIndex) }
        XCTAssertEqual(try Data(contentsOf: index), invalid)
    }

    func testRestartRecoversInterruptedSavesWithoutDeletingSavedOrUnrelatedFiles() async throws {
        let root = try directory()
        let library = CaptureLibrary(root: root)
        let saved = try await library.savePhoto(jpeg, cameraID: "cam_a", cameraName: "Camera")
        let savedURL = try await library.url(for: saved)
        let abandoned = try await library.reserveClip()
        try Data([1, 2, 3]).write(to: abandoned)
        let orphan = root.appendingPathComponent(UUID().uuidString + ".jpg")
        try jpeg.write(to: orphan)
        let unrelated = root.appendingPathComponent("user-note.txt")
        try Data([4]).write(to: unrelated)
        let recovered = try await CaptureLibrary(root: root).captures()
        XCTAssertEqual(recovered, [saved])
        XCTAssertEqual(try Data(contentsOf: savedURL), jpeg)
        XCTAssertTrue(FileManager.default.fileExists(atPath: unrelated.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: abandoned.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: orphan.path))
    }
}
