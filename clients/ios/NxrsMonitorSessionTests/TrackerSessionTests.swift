import Foundation
import UIKit
import XCTest
@testable import NxrsMonitor

@MainActor final class TrackerSessionTests: XCTestCase {
    private let dashboard = Data(("""
        {"v":1,"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"live","lat_deg":45,"lon_deg":-76,"speed_mps":2}}
        """ + "\n").utf8)

    func testActiveTrackerKeepsScreenAwakeOnlyInForegroundWhenEnabled() throws {
        let suite = "TrackerSessionTests.\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        let initial = UIApplication.shared.isIdleTimerDisabled
        defer {
            UIApplication.shared.isIdleTimerDisabled = initial
            defaults.removePersistentDomain(forName: suite)
        }
        let settings = HomeSettings(defaults: defaults)
        let model = MonitorModel(settings: settings)
        model.setTrackerActive(true)
        XCTAssertTrue(UIApplication.shared.isIdleTimerDisabled)
        model.setForeground(false)
        XCTAssertFalse(UIApplication.shared.isIdleTimerDisabled)
        model.setForeground(true)
        XCTAssertTrue(UIApplication.shared.isIdleTimerDisabled)
        model.setTrackerActive(false)
        XCTAssertFalse(UIApplication.shared.isIdleTimerDisabled)
        settings.preferences.keepScreenAwake = false
        model.setTrackerActive(true)
        XCTAssertFalse(UIApplication.shared.isIdleTimerDisabled)
    }

    func testTwoMinutesOfSensorBatchesKeepWorkingAfterTheDashboardExpires() throws {
        let tracker = TrackerConnection { _ in }
        let start = ContinuousClock.now
        XCTAssertTrue(tracker.receiveAuthenticatedData(dashboard, at: start))
        for first in stride(from: 0, to: 12_000, by: 4) {
            let at = start.advanced(by: .milliseconds(first * 10))
            let packet = batch(first..<(first + 4))
            XCTAssertTrue(tracker.receiveAuthenticatedData(Data(packet.prefix(3)), at: at))
            XCTAssertTrue(tracker.receiveAuthenticatedData(Data(packet.dropFirst(3)), at: at))
            XCTAssertTrue(tracker.refreshTelemetry(at: at))
        }
        // Publish the final counters after the graph refresh interval. A
        // repeated sequence never increments the received count.
        let end = start.advanced(by: .milliseconds(120_100))
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(11_999..<12_000), at: end))
        XCTAssertNil(tracker.presentation.reading, "Stale dashboard metadata clears while sensor batches continue")
        XCTAssertEqual(tracker.presentation.history.counts.map(\.received), [12_000, 12_000, 240])
        XCTAssertEqual(tracker.presentation.history.counts.map(\.dropped), [0, 0, 0])
        XCTAssertEqual(tracker.presentation.history.motion.map(\.count), [500, 500])
        XCTAssertEqual(tracker.presentation.history.positions.count, 240)
        XCTAssertEqual(try XCTUnwrap(tracker.presentation.history.counts[0].rate), 100, accuracy: 0.01)
        XCTAssertEqual(try XCTUnwrap(tracker.presentation.history.counts[2].rate), 2, accuracy: 0.01)
        XCTAssertTrue(tracker.refreshTelemetry(at: end.advanced(by: .seconds(12))))
        XCTAssertFalse(tracker.refreshTelemetry(at: end.advanced(by: .seconds(13))))
    }

    func testSampleGapMarksTheExportIncompleteAndRetirementResetsTheCounters() {
        let tracker = TrackerConnection { _ in }
        let start = ContinuousClock.now
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(0..<1), at: start))
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(2..<3), at: start.advanced(by: .seconds(1))))
        XCTAssertEqual(tracker.presentation.history.counts.map(\.dropped), [1, 1, 0])
        XCTAssertEqual(tracker.presentation.status, "Sensor export incomplete. Samples missing.")
        tracker.stop()
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(12_000..<12_001), at: start.advanced(by: .seconds(2))))
        XCTAssertEqual(tracker.presentation.history.counts.map(\.dropped), [0, 0, 0])
        XCTAssertEqual(tracker.presentation.history.counts.map(\.received), [1, 1, 1])
    }

    func testCaptureOverflowEndsTheExportWithAnExplicitStatus() {
        let tracker = TrackerConnection { _ in }
        let prefix = batch(0..<1)
        let firstMissing = Data([0xa5, 2, 5, 0, 2, 1, 0, 0, 0])
        XCTAssertFalse(tracker.receiveAuthenticatedData(prefix + firstMissing, at: .now))
        XCTAssertEqual(tracker.presentation.history.counts.map(\.received), [1, 1, 1])
        XCTAssertEqual(tracker.presentation.status, "Sensor export incomplete. First missing: Module IMU #1.")
    }

    func testDashboardTrafficDoesNotExtendSampleDeadline() {
        let tracker = TrackerConnection { _ in }
        let start = ContinuousClock.now
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(0..<1), at: start))
        let later = start.advanced(by: .seconds(13))
        XCTAssertTrue(tracker.receiveAuthenticatedData(dashboard, at: later))
        XCTAssertFalse(tracker.refreshTelemetry(at: later))
    }

    func testNotificationReplayReleasesRetainedCiphertextWhenTheGapCloses() {
        var stream = TrackerNotificationStream()
        XCTAssertEqual(stream.accept(Data([0, 0, 0, 0, 10, 11])), Data([10, 11]))
        XCTAssertNil(stream.accept(Data([4, 0, 0, 0, 14, 15])), "A gap must never reach TLS")
        XCTAssertEqual(stream.acknowledgement, Data([1, 2, 0, 0, 0]))
        XCTAssertNil(stream.accept(Data([4, 0, 0, 0, 14, 15])))
        XCTAssertEqual(stream.bufferedCount, 2)
        XCTAssertEqual(stream.accept(Data([1, 0, 0, 0, 11, 12, 13, 14])), Data([12, 13, 14, 15]))
        XCTAssertNil(stream.accept(Data([2, 0, 0, 0, 12, 13])))
        XCTAssertNil(stream.accept(Data([5, 0, 0, 0, 15])))
        XCTAssertEqual(stream.bufferedCount, 0)
        XCTAssertEqual(stream.acknowledgement, Data([1, 6, 0, 0, 0]))
    }

    func testNotificationWindowStaysBoundedAcrossOffsetWrap() {
        var stream = TrackerNotificationStream(offset: .max - 1)
        XCTAssertNil(stream.accept(Data([254, 7, 0, 0, 99])), "Outside the 2 KiB window")
        XCTAssertNil(stream.accept(Data([0, 0, 0, 0, 3, 4])))
        XCTAssertNil(stream.accept(Data([4, 0, 0, 0, 7])))
        XCTAssertEqual(stream.accept(Data([254, 255, 255, 255, 1, 2])), Data([1, 2, 3, 4]))
        XCTAssertEqual(stream.acknowledgement, Data([1, 2, 0, 0, 0]))
        XCTAssertEqual(stream.bufferedCount, 1, "A second gap must stay buffered")
        XCTAssertEqual(stream.accept(Data([2, 0, 0, 0, 5, 6])), Data([5, 6, 7]))
        XCTAssertEqual(stream.acknowledgement, Data([1, 5, 0, 0, 0]))
    }

    private func batch(_ ticks: Range<Int>) -> Data {
        var records = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            var word = value.littleEndian
            withUnsafeBytes(of: &word) { records.append(contentsOf: $0) }
        }
        for tick in ticks {
            for source: UInt8 in [1, 2] {
                records.append(source)
                append(UInt32(tick))
                append(UInt32(tick * 10))
                for _ in 0..<6 { append(Float(source).bitPattern) }
            }
            if tick % 50 == 0 {
                records.append(3)
                append(UInt32(tick / 50))
                append(UInt32(tick * 10))
                append(Double(45).bitPattern)
                append(Double(-76).bitPattern)
                append(Float(2).bitPattern)
            }
        }
        return Data([0xa5, 1, UInt8(truncatingIfNeeded: records.count), UInt8(records.count >> 8)]) + records
    }
}
