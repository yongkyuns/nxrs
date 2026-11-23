import Foundation
import XCTest
@testable import NxrsMonitor

@MainActor final class TrackerSessionTests: XCTestCase {
    private let dashboard = Data(("""
        {"v":1,"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"live","lat_deg":45,"lon_deg":-76,"speed_mps":2}}
        """ + "\n").utf8)

    func testTwoMinutesOfSensorBatchesKeepWorkingAfterTheDashboardExpires() throws {
        let tracker = TrackerBluetooth()
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
        XCTAssertNil(tracker.reading, "Stale dashboard metadata clears while sensor batches continue")
        XCTAssertEqual(tracker.history.counts.map(\.received), [12_000, 12_000, 240])
        XCTAssertEqual(tracker.history.counts.map(\.dropped), [0, 0, 0])
        XCTAssertEqual(tracker.history.motion.map(\.count), [500, 500])
        XCTAssertEqual(tracker.history.positions.count, 240)
        XCTAssertEqual(try XCTUnwrap(tracker.history.counts[0].rate), 100, accuracy: 0.01)
        XCTAssertEqual(try XCTUnwrap(tracker.history.counts[2].rate), 2, accuracy: 0.01)
        XCTAssertTrue(tracker.refreshTelemetry(at: end.advanced(by: .seconds(12))))
        XCTAssertFalse(tracker.refreshTelemetry(at: end.advanced(by: .seconds(13))))
    }

    func testSampleGapMarksTheExportIncompleteAndRetirementResetsTheCounters() {
        let tracker = TrackerBluetooth()
        let start = ContinuousClock.now
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(0..<1), at: start))
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(2..<3), at: start.advanced(by: .seconds(1))))
        XCTAssertEqual(tracker.history.counts.map(\.dropped), [1, 1, 0])
        XCTAssertEqual(tracker.status, "Sensor export incomplete. Samples missing.")
        tracker.stop()
        XCTAssertTrue(tracker.receiveAuthenticatedData(batch(12_000..<12_001), at: start.advanced(by: .seconds(2))))
        XCTAssertEqual(tracker.history.counts.map(\.dropped), [0, 0, 0])
        XCTAssertEqual(tracker.history.counts.map(\.received), [1, 1, 1])
    }

    func testCaptureOverflowEndsTheExportWithAnExplicitStatus() {
        let tracker = TrackerBluetooth()
        XCTAssertFalse(tracker.receiveAuthenticatedData(Data([0xa5, 1, 0, 0]), at: .now))
        XCTAssertEqual(tracker.status, "Sensor export incomplete. Tracker buffer filled.")
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
