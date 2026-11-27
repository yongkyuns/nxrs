import Foundation
import XCTest
@testable import NxrsTrackerCore

final class TrackerLifecycleTests: XCTestCase {
    private func word<T: FixedWidthInteger>(_ value: T) -> Data {
        var little = value.littleEndian
        return withUnsafeBytes(of: &little) { Data($0) }
    }

    private func frame(_ payload: Data, version: UInt8 = 4) -> Data {
        Data([0xa5, version]) + word(UInt16(payload.count)) + payload
    }

    private func noFix(generation: UInt32 = 9, time: UInt64 = 120, epoch: UInt64 = 5_000) -> Data {
        Data([4]) + word(generation) + word(time) + word(epoch)
    }

    private func imu(tag: UInt8 = 1, sequence: UInt32 = 7, time: UInt64 = 100) -> Data {
        var record = Data([tag]) + word(sequence) + word(time)
        for value: Float in [1, -1, 0, 1, -1, 0] { record.append(word(value.bitPattern)) }
        return record
    }

    private func hex(_ value: String) -> Data {
        var data = Data()
        var start = value.startIndex
        while start < value.endIndex {
            let end = value.index(start, offsetBy: 2)
            data.append(UInt8(value[start..<end], radix: 16)!)
            start = end
        }
        return data
    }

    func testRustNoFixGoldenReachesTheTypedLifecycleRecord() throws {
        let golden = hex("a5041d0000000000000000800407000000e803000000000000f401000000000000")
        var parser = TrackerExportFrames()
        var actual: [TrackerMeasurement] = []
        try parser.receive(golden, snapshot: { _ in XCTFail() }, measurement: { actual.append($0) })
        XCTAssertEqual(actual, [.noFix(generation: 7, receivedMS: 1_000, sourceEpochMS: 500)])
    }

    func testReceivedAndUnavailableDashboardStatesPreserveMeaningAndSelectV4() throws {
        let line = Data((#"{"v":1,"export_version":4,"output_faults":15,"imu":[{"state":"received","seq":3,"received_ms":90,"accel_mps2":[0,0,9.8],"gyro_rps":[0,0,0]},{"state":"unavailable"}],"gnss":{"state":"unavailable"}}"# + "\n").utf8)
        var parser = TrackerFrames()
        var snapshots: [TrackerTelemetry] = []
        for byte in line { try parser.receive(Data([byte])) { snapshots.append($0) } }
        let snapshot = try XCTUnwrap(snapshots.first)
        XCTAssertEqual(snapshot.imu[0].state, .received)
        XCTAssertEqual(snapshot.gnss.state, .unavailable)
        XCTAssertNil(snapshot.gnss.latitude)
        XCTAssertEqual(snapshot.outputFaults, 15)
        XCTAssertEqual(TrackerExportFrames.preferredRequest(for: snapshot), Data([78, 88, 82, 83, 4]))
        let legacy = try JSONDecoder().decode(TrackerTelemetry.self, from: Data(
            #"{"v":1,"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"waiting"}}"#.utf8))
        XCTAssertEqual(legacy.outputFaults, 0)
        XCTAssertEqual(TrackerExportFrames.preferredRequest(for: legacy), TrackerExportFrames.request)
        for invalid in [
            #"{"v":1,"imu":[{"state":"received"},{"state":"waiting"}],"gnss":{"state":"waiting"}}"#,
            #"{"v":1,"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"unavailable","lat_deg":0}}"#
        ] {
            var invalidParser = TrackerFrames()
            XCTAssertThrowsError(try invalidParser.receive(Data((invalid + "\n").utf8)) { _ in XCTFail() })
        }
    }

    func testV4BatchPreservesNoFixAndPerRecordTimingProvenance() throws {
        var payload = word(Int64(1_000))
        payload.append(imu(tag: 0x31))
        payload.append(noFix())
        payload.append(contentsOf: [0x81, 10])
        for value: Float in [1, -1, 0, 1, -1, 0] { payload.append(word(value.bitPattern)) }
        let bytes = frame(payload)
        var parser = TrackerExportFrames()
        var records: [TrackerMeasurement] = []
        for byte in bytes {
            try parser.receive(Data([byte]), snapshot: { _ in XCTFail() }, measurement: { records.append($0) })
        }
        XCTAssertEqual(records, [
            .imu(index: 0, sequence: 7, receivedMS: 100, accel: [1, -1, 0], gyro: [1, -1, 0], provenance: .received),
            .noFix(generation: 9, receivedMS: 120, sourceEpochMS: 5_000),
            .imu(index: 0, sequence: 8, receivedMS: 110, accel: [1, -1, 0], gyro: [1, -1, 0], utcMS: 1_110, provenance: .capture)
        ])
    }

    func testUnknownUtcAndUnknownSourceEpochRemainUnknown() throws {
        var parser = TrackerExportFrames()
        var records: [TrackerMeasurement] = []
        try parser.receive(frame(word(Int64.min) + imu() + noFix(epoch: .max)),
                           snapshot: { _ in XCTFail() }, measurement: { records.append($0) })
        XCTAssertEqual(records, [
            .imu(index: 0, sequence: 7, receivedMS: 100, accel: [1, -1, 0], gyro: [1, -1, 0], provenance: .capture),
            .noFix(generation: 9, receivedMS: 120, sourceEpochMS: nil)
        ])
        records.removeAll()
        try parser.receive(frame(word(Int64(1_000)) + imu(tag: 0x21)),
                           snapshot: { _ in XCTFail() }, measurement: { records.append($0) })
        XCTAssertEqual(records, [.imu(index: 0, sequence: 7, receivedMS: 100,
                                     accel: [1, -1, 0], gyro: [1, -1, 0], provenance: .capture)])
    }

    func testEveryTypedTerminalPreservesTheAlreadyAcceptedPrefix() throws {
        for source in UInt8(1)...3 {
            for cause in UInt8(1)...7 {
                var parser = TrackerExportFrames()
                var records: [TrackerMeasurement] = []
                let prefix = frame(word(Int64.min) + imu())
                try parser.receive(prefix + Data([0xa5, 4, 2, 0, source]),
                                   snapshot: { _ in XCTFail() }, measurement: { records.append($0) })
                XCTAssertEqual(records.count, 1)
                XCTAssertThrowsError(try parser.receive(Data([cause]), snapshot: { _ in XCTFail() },
                                                        measurement: { _ in XCTFail() })) { error in
                    guard case let TrackerFrames.FrameError.sourceFailure(fault) = error else {
                        return XCTFail("Expected typed source failure")
                    }
                    XCTAssertEqual(fault.source.rawValue, source)
                    XCTAssertEqual(fault.cause.rawValue, cause)
                }
            }
        }
    }

    func testV4RejectsInvalidFlagsLengthsSourcesAndCausesAtomically() {
        var invalid: [Data] = []
        for tag: UInt8 in [0x14, 0x24, 0x44, 0x84] {
            var record = noFix()
            record[0] = tag
            invalid.append(frame(word(Int64.min) + record))
        }
        invalid.append(frame(word(Int64.min) + noFix().dropLast()))
        invalid.append(frame(word(Int64.min) + noFix() + Data([0])))
        invalid.append(frame(word(Int64.min) + (0..<17).reduce(Data()) { bytes, _ in bytes + noFix() }))
        invalid.append(frame(word(Int64.min) + imu(tag: 5)))
        invalid.append(frame(word(Int64.min) + imu(tag: 0x43)))
        invalid.append(frame(word(Int64.min) + imu(tag: 0x11)))
        invalid.append(frame(word(Int64(1_000)) + imu(tag: 0x11)))
        invalid.append(frame(Data([0, 1])))
        invalid.append(frame(Data([4, 1])))
        invalid.append(frame(Data([1, 0])))
        invalid.append(frame(Data([1, 8])))
        invalid.append(Data([0xa5, 4, 255, 255]))
        for bytes in invalid {
            var parser = TrackerExportFrames()
            XCTAssertThrowsError(try parser.receive(bytes, snapshot: { _ in XCTFail() },
                                                    measurement: { _ in XCTFail("Invalid batch emitted a prefix") }))
        }
    }
}
