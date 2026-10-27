import XCTest
import SwiftUI
import UIKit
import AVFoundation
import NxrsMonitorCore
import NxrsMonitorNative
@testable import NxrsMonitor

@MainActor final class CameraSessionTests: XCTestCase {
    private struct Fixture: Decodable { let gateway: URL; let mediaOrigin: URL; let token: String }
    private func until(_ label: String, seconds: Double = 30, diagnostics: (@MainActor () -> String)? = nil, _ condition: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + seconds
        while !condition(), ProcessInfo.processInfo.systemUptime < deadline { try await Task.sleep(for: .milliseconds(50)) }
        if !condition(), let diagnostics { print("CAMERA DIAGNOSTIC: " + diagnostics()) }
        XCTAssertTrue(condition(), label)
    }

    private func camera(_ id: String) throws -> CameraSummary {
        let json = """
        {"camera_id":"\(id)","gateway_id":"gateway","site_id":"site","name":"\(id)","source":"synthetic","availability":"unknown","live_video":true}
        """
        return try JSONDecoder().decode(CameraSummary.self, from: Data(json.utf8))
    }

    func testCatalogRemovalSelectsRemainingCameraAndClosesObsoleteFullscreen() throws {
        let suite = "org.nxrs.tests.catalog." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let model = MonitorModel(settings: HomeSettings(defaults: defaults))
        let a = try camera("cam_a"), b = try camera("cam_b")
        model.updateCatalog([a, b])
        let original = try XCTUnwrap(model.sessions[a.cameraID])
        model.setFullscreen(b)
        XCTAssertEqual(model.selected?.cameraID, b.cameraID)
        model.updateCatalog([a])
        XCTAssertEqual(model.selected?.cameraID, a.cameraID)
        XCTAssertNil(model.fullscreen)
        XCTAssertTrue(model.sessions[a.cameraID] === original, "Unchanged camera keeps its session")
        XCTAssertNil(model.sessions[b.cameraID])
        model.setFullscreen(a)
        model.updateCatalog([])
        XCTAssertNil(model.selected)
        XCTAssertNil(model.fullscreen)
        XCTAssertTrue(model.sessions.isEmpty)
    }

    func testCatalogAdditionKeepsSelectedCameraSession() throws {
        let suite = "org.nxrs.tests.catalog." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let model = MonitorModel(settings: HomeSettings(defaults: defaults))
        let a = try camera("cam_a"), b = try camera("cam_b")
        model.updateCatalog([a])
        let original = try XCTUnwrap(model.sessions[a.cameraID])
        model.updateCatalog([a, b])
        XCTAssertEqual(model.selected?.cameraID, a.cameraID)
        XCTAssertTrue(model.sessions[a.cameraID] === original)
        XCTAssertNotNil(model.sessions[b.cameraID])
    }

    func testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras() async throws {
        guard let path = ProcessInfo.processInfo.environment["NXRS_CLIENT_FIXTURE"], !path.isEmpty else {
            throw XCTSkip("Requires the isolated multi-camera runner")
        }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        let suite = "org.nxrs.tests.manual-playback." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = HomeSettings(defaults: defaults)
        settings.use(gateway: fixture.gateway)
        settings.preferences.autoConnect = false
        let model = MonitorModel(settings: settings)
        defer { model.setForeground(false); model.forget() }
        model.enroll(gateway: fixture.gateway.absoluteString, media: fixture.mediaOrigin.absoluteString, token: fixture.token)
        try await until("Manual enrollment completes") { !model.pairing }
        XCTAssertNil(model.pairingError)
        let a = try XCTUnwrap(model.cameras.first)
        XCTAssertEqual(model.selected?.cameraID, a.cameraID, "A camera stays selectable without autoplay")
        for camera in model.cameras { model.setVisible(camera, visible: true) }
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertTrue(model.sessions.values.allSatisfy { !$0.occupiesSlot })
        model.resume(a)
        try await until("Explicit Watch starts the chosen camera") { model.sessions[a.cameraID]?.live == true }
        XCTAssertEqual(model.sessions.values.filter(\.occupiesSlot).count, 1)
        model.setForeground(false)
        try await until("Manual playback retires cleanly") { model.sessions.values.allSatisfy { !$0.occupiesSlot } }
    }
    func testIndependentNativeCamerasAndCaptureRetirement() async throws {
        guard let path = ProcessInfo.processInfo.environment["NXRS_CLIENT_FIXTURE"], !path.isEmpty else {
            throw XCTSkip("Requires clients/tests/multicamera_reference.py with the isolated iOS runner")
        }
        continueAfterFailure = false
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        XCTAssertEqual(fixture.gateway.host, "localhost")
        do {
            _ = try await GatewayClient(policy: EndpointPolicy(gateway: fixture.gateway, additionalMediaOrigins: [fixture.mediaOrigin])).cameras(credential: BearerCredential(fixture.token))
        } catch {
            let native = error as NSError
            XCTFail("Fixture HTTPS admission failed: \(native.domain) \(native.code)")
            return
        }
        let model = MonitorModel.onboardingPreview()
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = try XCTUnwrap(scene.windows.first { $0.isKeyWindow })
        let previous = window.rootViewController
        window.rootViewController = UIHostingController(rootView: HomeDashboard(model: model, settings: model.settings))
        defer { model.setForeground(false); model.forget(); window.rootViewController = previous }
        model.enroll(gateway: fixture.gateway.absoluteString, media: fixture.mediaOrigin.absoluteString, token: fixture.token)
        try await until("Invitation verification completes") { !model.pairing }
        XCTAssertNil(model.pairingError, "Invitation could not be saved: \(model.pairingError ?? "")")
        XCTAssertEqual(model.cameras.count, 3)
        let a = try XCTUnwrap(model.cameras.first { $0.cameraID.value == "cam_a" })
        let b = try XCTUnwrap(model.cameras.first { $0.cameraID.value == "cam_b" })
        let c = try XCTUnwrap(model.cameras.first { $0.cameraID.value == "cam_c" })
        model.setVisible(a, visible: true); model.setVisible(b, visible: true); model.setVisible(c, visible: true)
        try await until("Two real camera decoders advance concurrently", diagnostics: {
            [a,b].map { camera in
                guard let session = model.sessions[camera.cameraID] else { return camera.cameraID.value + ": missing" }
                return "\(camera.cameraID.value): status=\(session.status) active=\(session.active) retiring=\(session.retiring) blocked=\(session.blocked) frames=\(session.peer?.frames.count ?? 0)"
            }.joined(separator: "; ")
        }) {
            [a,b].allSatisfy { model.sessions[$0.cameraID]?.live == true && (model.sessions[$0.cameraID]?.peer?.frames.count ?? 0) >= 20 }
        }
        XCTAssertEqual(model.sessions.values.filter(\.occupiesSlot).count, 2)
        let sa = try XCTUnwrap(model.sessions[a.cameraID]); let sb = try XCTUnwrap(model.sessions[b.cameraID])
        XCTAssertFalse(sa.peer === sb.peer)
        let epochA = try XCTUnwrap(sa.peer?.frames.videoMetadata?.epoch)
        let epochB = try XCTUnwrap(sb.peer?.frames.videoMetadata?.epoch)
        XCTAssertNotEqual(epochA, epochB, "Each publication has an independent source namespace")
        let peerB = try XCTUnwrap(sb.peer); let beforeB = peerB.frames.count
        model.pause(a)
        try await until("Pausing one camera leaves its neighbor advancing") { !sa.occupiesSlot && peerB.frames.count > beforeB + 10 }
        XCTAssertNil(sa.peer); XCTAssertFalse(sa.live); XCTAssertEqual(sa.detectionStatus, "Detection unavailable")
        model.setFullscreen(b)
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertTrue(sb.peer === peerB, "Full screen reuses the selected peer")
        model.setFullscreen(nil); model.resume(a)
        try await until("Resume selected camera") { sa.live }
        sa.takeSnapshot(); sb.toggleRecording()
        try await until("Snapshot saved independently") { model.library.captures.contains { $0.cameraID == a.cameraID.value } }
        try await until("Other camera recording") { sb.isRecording }
        try await Task.sleep(for: .seconds(2))
        sb.toggleRecording()
        try await until("Clip finalization joined") { !sb.isRecording && !sb.finalizingClip }
        XCTAssertTrue(model.library.captures.contains { $0.cameraID == b.cameraID.value && $0.kind == .clip })
        // Prioritize a third camera only after the obsolete peer and recording
        // resources retire; this also exercises cancellation during admission.
        model.select(c)
        try await until("Third camera replaces the least-priority session") { model.sessions[c.cameraID]?.live == true && model.sessions.values.filter(\.occupiesSlot).count == 2 }
        let current = model.sessions.values.compactMap(\.peer)
        model.setForeground(false)
        XCTAssertTrue(current.allSatisfy(\.isClosed))
        try await until("Background joins all retiring resources") { model.sessions.values.allSatisfy { !$0.occupiesSlot } }
        XCTAssertTrue(model.sessions.values.allSatisfy { $0.peer == nil && !$0.live && $0.detectionStatus == "Detection unavailable" })
        model.setForeground(true)
        try await until("Foreground restores bounded viewing") { model.sessions.values.filter(\.live).count == 2 }
        // Repeat this transition within the existing runner/startup budgets.
        // Two cycles also leave headroom in the unchanged finite grant pool
        // for the later manual-playback case; this is not an unlimited soak.
        for removalCase in 1...2 {
            if removalCase > 1 {
                model.updateCatalog([a,b,c])
                model.setVisible(c, visible: true)
                model.select(c)
                try await until("Reinserted camera advances before removal") {
                    model.sessions[c.cameraID]?.live == true && model.sessions.values.filter(\.occupiesSlot).count == 2
                }
            }
            let removedPeer = try XCTUnwrap(model.sessions[c.cameraID]?.peer)
            model.setFullscreen(c)
            model.updateCatalog([a,b])
            XCTAssertNil(model.fullscreen)
            XCTAssertEqual(model.selected?.cameraID, a.cameraID)
            XCTAssertTrue(removedPeer.isClosed, "Removed camera closes its local decoder immediately")
            try await until("Remaining cameras recover after removed camera retirement (\(removalCase)/2)", diagnostics: {
                // Keep this fixture witness free of URLs, credentials and native
                // error text. The runner admits only this bounded numeric shape.
                @MainActor func state(_ camera: CameraSummary) -> String {
                    guard let session = model.sessions[camera.cameraID] else { return "0,0,0,0,0,0" }
                    return "1,\(session.live ? 1 : 0),\(session.active ? 1 : 0),\(session.retiring ? 1 : 0),\(session.blocked ? 1 : 0),\(session.peer?.frames.count ?? 0)"
                }
                return "IOS RETIREMENT STATE: sessions=\(model.sessions.count) a=\(state(a)) b=\(state(b))"
            }) {
                model.sessions.count == 2 && model.sessions.values.allSatisfy(\.live)
            }
        }
        print("MULTICAMERA IOS PASS: two native decoders; independent epochs/pause; full-screen reuse; JPEG/clip capture; third-camera admission; background retirement; removed-camera cleanup (two consecutive cycles)")
    }
}
