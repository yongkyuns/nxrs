import XCTest
import SwiftUI
import UIKit
import NxrsMonitorCore
import NxrsMonitorNative
@testable import NxrsMonitor

@MainActor final class PhoneHostTests: XCTestCase {
    private func until(_ seconds: Double = 40, _ condition: () -> Bool) async throws -> Bool {
        let deadline = ProcessInfo.processInfo.systemUptime + seconds
        while !condition(), ProcessInfo.processInfo.systemUptime < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        return condition()
    }
    func testPhysicalDetectionPlaybackAndLifecycle() async throws {
        continueAfterFailure = false
        let model = MonitorModel(); defer { model.stop() }
        model.restore()
        let catalog = try await until { model.enrolled && !model.busy && !model.cameras.isEmpty }
        XCTAssertTrue(catalog, "Existing trusted enrollment must load the catalog")
        let camera = try XCTUnwrap(model.cameras.first { $0.source.rawValue == "physical" })
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = try XCTUnwrap(scene.windows.first { $0.isKeyWindow })
        let previous = window.rootViewController
        window.rootViewController = UIHostingController(rootView: MonitorView(model: model))
        defer { window.rootViewController = previous }
        model.select(camera)
        let ready = try await until { model.live && (model.peer?.frames.count ?? 0) >= 10 }
        XCTAssertTrue(ready, "Physical iPhone must decode advancing video")
        let peer = try XCTUnwrap(model.peer)
        let first = peer.frames.count, start = ProcessInfo.processInfo.systemUptime
        var sampled = 0, mapped = 0, count = first, observations = Set<UInt64>()
        while ProcessInfo.processInfo.systemUptime - start < 60 {
            let frame = peer.frames
            if frame.count != count {
                count = frame.count; sampled += 1
                if let metadata = frame.videoMetadata {
                    mapped += 1
                    if let latest = metadata.source?.latest { observations.insert(latest.frame_sequence) }
                }
            }
            XCTAssertTrue(model.live)
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let seconds = ProcessInfo.processInfo.systemUptime - start
        let decoded = peer.frames.count - first
        XCTAssertGreaterThan(Double(decoded)/seconds, 10)
        XCTAssertGreaterThanOrEqual(mapped*100, sampled*95)
        XCTAssertGreaterThan(observations.count, 50)
        XCTAssertNotEqual(model.detectionStatus, "Detection unavailable")
        print("PHONE_SAMPLE {\"seconds\":\(seconds),\"decoded_frames\":\(decoded),\"decoded_fps\":\(Double(decoded)/seconds),\"sampled_frames\":\(sampled),\"mapped_frames\":\(mapped),\"observations\":\(observations.count)}")
        model.stop()
        XCTAssertFalse(model.live); XCTAssertNil(model.peer); XCTAssertTrue(peer.isClosed)
        XCTAssertEqual(model.detectionStatus, "Detection unavailable")
        model.select(camera)
        let resumed = try await until { model.live && (model.peer?.frames.count ?? 0) >= 5 }
        XCTAssertTrue(resumed); XCTAssertFalse(model.peer === peer)
        let foregroundPeer = try XCTUnwrap(model.peer)
        model.setForeground(false)
        XCTAssertFalse(model.live); XCTAssertTrue(foregroundPeer.isClosed)
        XCTAssertEqual(model.detectionStatus, "Detection unavailable")
        model.setForeground(true)
        let restored = try await until { model.live && (model.peer?.frames.count ?? 0) >= 5 }
        XCTAssertTrue(restored)
        print("PHONE PASS: physical decode, matched frame metadata, pause clearing, fresh-peer restart and foreground policy callbacks")
    }
}
