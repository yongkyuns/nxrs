import XCTest
import SwiftUI
import UIKit
import AVFoundation
import NxrsMonitorCore
import NxrsMonitorNative
@testable import NxrsMonitor

@MainActor final class CameraSessionTests: XCTestCase {
    private struct Fixture: Decodable { let gateway: URL; let mediaOrigin: URL; let token: String }
    private func until(_ label: String, seconds: Double = 30, _ condition: () -> Bool) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + seconds
        while !condition(), ProcessInfo.processInfo.systemUptime < deadline { try await Task.sleep(for: .milliseconds(50)) }
        XCTAssertTrue(condition(), label)
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
        model.gridMode = true
        model.enroll(gateway: fixture.gateway.absoluteString, media: fixture.mediaOrigin.absoluteString, token: fixture.token)
        try await until("Invitation verification completes") { !model.pairing }
        XCTAssertNil(model.pairingError, "Invitation could not be saved: \(model.pairingError ?? "")")
        XCTAssertEqual(model.cameras.count, 3)
        let a = try XCTUnwrap(model.cameras.first { $0.cameraID.value == "cam_a" })
        let b = try XCTUnwrap(model.cameras.first { $0.cameraID.value == "cam_b" })
        let c = try XCTUnwrap(model.cameras.first { $0.cameraID.value == "cam_c" })
        model.setVisible(a, visible: true); model.setVisible(b, visible: true); model.setVisible(c, visible: true)
        try await until("Two real camera decoders advance concurrently") {
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
        print("MULTICAMERA IOS PASS: two native decoders; independent epochs/pause; full-screen reuse; JPEG/clip capture; third-camera admission; background retirement")
    }
}
