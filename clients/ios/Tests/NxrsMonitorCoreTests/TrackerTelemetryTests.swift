import Foundation
import XCTest
@testable import NxrsMonitorCore

final class TrackerTelemetryTests: XCTestCase {
    func testSplitFramesPreserveIndependentSensorsAndRejectUnknownVersions() throws {
        let waiting = #"{"v":1,"imu":[{"state":"waiting"},{"state":"fault"}],"gnss":{"state":"stale"}}"#
        let live = #"{"v":1,"imu":[{"state":"live","seq":4,"accel_mps2":[0,1,9.8],"gyro_rps":[0,0,0]},{"state":"waiting"}],"gnss":{"state":"live","lat_deg":45,"lon_deg":-80,"speed_mps":1}}"#
        var reader = TrackerFrames()
        var output = [TrackerTelemetry]()
        for byte in (waiting + "\n" + live + "\n").utf8 {
            try reader.receive(Data([byte])) { output.append($0) }
        }
        XCTAssertEqual(output.count, 2)
        XCTAssertEqual(output[0].imu[1].state, .fault)
        XCTAssertEqual(output[0].gnss.latitude, nil)
        XCTAssertEqual(output[1].imu[0].sequence, 4)
        XCTAssertEqual(output[1].gnss.longitude, -80)
        XCTAssertThrowsError(try reader.receive(Data((waiting.replacingOccurrences(of: "\"v\":1", with: "\"v\":2") + "\n").utf8)) { _ in })
        XCTAssertThrowsError(try reader.receive(Data(repeating: 0x31, count: 768)) { _ in })
    }
}

extension TrackerTelemetryTests {
    func testExportDecodesSplitBatchAndPreservesDashboardFraming() throws {
        var binary = Data([0xa5, 1, 62, 0])
        func append<T: FixedWidthInteger>(_ value: T) {
            var word = value.littleEndian
            withUnsafeBytes(of: &word) { binary.append(contentsOf: $0) }
        }
        binary.append(2)
        append(UInt32(10))
        append(UInt32(100))
        for value: Float in [1, 2, 3, -1, -2, -3] { append(value.bitPattern) }
        binary.append(3)
        append(UInt32(7))
        append(UInt32(500))
        append(Double(45.25).bitPattern)
        append(Double(-75.125).bitPattern)
        append(Float(2.5).bitPattern)
        let snapshot = Data("""
        {"v":1,"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"waiting"}}
        """.utf8) + Data([10])

        var parser = TrackerExportFrames()
        var samples: [TrackerMeasurement] = []
        var snapshots = 0
        try parser.receive(binary.prefix(16), snapshot: { _ in XCTFail() }, measurement: { _ in XCTFail() })
        try parser.receive(Data(binary.dropFirst(16)) + snapshot, snapshot: { _ in snapshots += 1 },
                           measurement: { samples.append($0) })
        XCTAssertEqual(snapshots, 1)
        XCTAssertEqual(samples, [
            .imu(index: 1, sequence: 10, receivedMS: 100, accel: [1, 2, 3], gyro: [-1, -2, -3]),
            .fix(sequence: 7, receivedMS: 500, latitude: 45.25, longitude: -75.125, speed: 2.5)
        ])
    }

    func testExportRejectsOversizedAndMalformedBatch() throws {
        for bytes in [Data([0xa5, 1, 255, 255]), Data([0xa5, 1, 29, 0] + [UInt8](repeating: 0, count: 29))] {
            var parser = TrackerExportFrames()
            XCTAssertThrowsError(try parser.receive(bytes, snapshot: { _ in XCTFail() }, measurement: { _ in XCTFail() }))
        }
    }
}
