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
