import Foundation
import XCTest
@testable import NxrsTrackerCore

final class TrackerRecordingTests: XCTestCase {
    private let imu = TrackerMeasurement.imu(index: 0, sequence: 10, receivedMS: 100,
                                              accel: [1, 0, 9.8], gyro: [0, 1, 0], utcMS: 1_000)

    func testRecordsSourceTimestampsGapsAndReconnects() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }

        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            recorder.append([imu, imu,
                             .imu(index: 0, sequence: 13, receivedMS: 100, accel: [2, 0, 9.8], gyro: [0, 0, 0]),
                             .fix(sequence: 3, receivedMS: 110, latitude: 45, longitude: -76, speed: 2)])
            recorder.interrupt(.reconnecting)
            recorder.resume()
            recorder.append([.imu(index: 1, sequence: 50, receivedMS: 150, accel: [-2, 0, 9.8], gyro: [0, 0, 0])])
            recorder.finish()
        }

        let rows = try read(saved.url)
        XCTAssertEqual(try TrackerRecording.files(in: folder).map(\.lastPathComponent), [saved.url.lastPathComponent])
        XCTAssertFalse(saved.complete)
        XCTAssertEqual(rows.compactMap { $0["type"] as? String },
                       ["header", "imu", "imu", "gnss", "interruption", "resume", "imu", "end"])
        XCTAssertEqual(rows[1]["utc_ms"] as? Int, 1_000)
        XCTAssertEqual(rows[0]["version"] as? Int, 2)
        XCTAssertEqual(rows[2]["timestamp_ms"] as? Int, 100)
        XCTAssertEqual(rows[2]["timestamp_provenance"] as? String, "unknown")
        XCTAssertEqual(rows[2]["missed_before"] as? Int, 2)
        XCTAssertEqual(rows[3]["lon_deg"] as? Int, -76)
        XCTAssertEqual(rows[6]["source"] as? String, "imu_secondary")
        XCTAssertEqual(rows.last?["complete"] as? Bool, false)
    }

    func testWriterOverrunFinalizesTheAcceptedPrefix() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }

        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            recorder.append([imu])
            recorder.append(Array(repeating: imu, count: 1_025))
            recorder.finish()
        }

        let rows = try read(saved.url)
        XCTAssertFalse(saved.complete)
        XCTAssertEqual(rows.count, 3)
        XCTAssertEqual(rows.last?["reason"] as? String, "writer_overrun")
    }

    func testStoppingBeforeAnySamplesLeavesNoArchive() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }

        let result: Result<TrackerRecording.Saved, Error> = await withCheckedContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(returning: $0) }
            recorder.finish()
        }
        XCTAssertThrowsError(try result.get()) { error in
            XCTAssertEqual(error as? TrackerRecording.WriteError, .noSamples)
        }
        XCTAssertTrue(try TrackerRecording.files(in: folder).isEmpty)
    }

    func testNoFixAndTimingBasisChangesPreserveACompleteRecording() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            recorder.append([
                .fix(sequence: 1, receivedMS: 100, latitude: 45, longitude: -76, speed: 2, provenance: .received),
                .noFix(generation: 3, receivedMS: 101, sourceEpochMS: nil),
                .imu(index: 0, sequence: 1, receivedMS: 100, accel: [0, 0, 9.8], gyro: [0, 0, 0], provenance: .capture),
                .fix(sequence: 2, receivedMS: 99, latitude: 45, longitude: -76, speed: 2, provenance: .capture),
                .fix(sequence: 3, receivedMS: 102, latitude: 45, longitude: -76, speed: 2, provenance: .received)
            ])
            recorder.finish()
        }
        let rows = try read(saved.url)
        XCTAssertTrue(saved.complete)
        XCTAssertEqual(rows.compactMap { $0["type"] as? String },
                       ["header", "gnss", "no_fix", "imu", "gnss", "gnss", "end"])
        XCTAssertEqual(rows[1]["timestamp_provenance"] as? String, "received")
        XCTAssertEqual(rows[4]["timestamp_provenance"] as? String, "capture")
        XCTAssertEqual(rows[2]["generation"] as? Int, 3)
        XCTAssertEqual(rows[2]["received_ms"] as? Int, 101)
        XCTAssertNil(rows[2]["source_epoch_ms"])
    }

    func testSourceFailureSealsAdmissionAndEndsAfterTheAcceptedPrefix() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            recorder.append([imu, .noFix(generation: 7, receivedMS: 110, sourceEpochMS: 500)])
            recorder.sourceFailed(.init(source: .gnss, cause: .lostAvailabilityEvidence))
            recorder.append([.imu(index: 1, sequence: 11, receivedMS: 120, accel: [0, 0, 0], gyro: [0, 0, 0])])
            recorder.finish(.stopped)
        }
        let rows = try read(saved.url)
        XCTAssertFalse(saved.complete)
        XCTAssertEqual(rows.compactMap { $0["type"] as? String }, ["header", "imu", "no_fix", "source_failure", "end"])
        XCTAssertEqual(rows[3]["source"] as? String, "gnss")
        XCTAssertEqual(rows[3]["reason"] as? String, "lost_availability_evidence")
        XCTAssertEqual(rows.last?["reason"] as? String, "source_failure")
        XCTAssertEqual(rows.last?["complete"] as? Bool, false)
    }

    func testDecodedV4PrefixAndFaultRoundTripIntoArchiveEvidence() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        var payload = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { payload.append(contentsOf: $0) }
        }
        append(Int64.min)
        payload.append(0x31) // Receipt time, no capture-qualified UTC.
        append(UInt32(9))
        append(UInt64(1000))
        for value: Float in [1.25, -0.5, 9.75, 0, 1, -2] { append(value.bitPattern) }
        payload.append(4)
        append(UInt32(7))
        append(UInt64(1001))
        append(UInt64(500))
        let bytes = Data([0xa5, 4, UInt8(payload.count), 0]) + payload + Data([0xa5, 4, 2, 0, 1, 3])
        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            var parser = TrackerExportFrames()
            var prefix: [TrackerMeasurement] = []
            do {
                try parser.receive(bytes, snapshot: { _ in XCTFail() }, measurement: { prefix.append($0) })
                XCTFail("Expected the typed terminal")
                recorder.finish(.invalidStream)
            } catch let TrackerFrames.FrameError.sourceFailure(fault) {
                recorder.append(prefix)
                recorder.sourceFailed(fault)
                recorder.finish(.stopped)
            } catch {
                XCTFail("Unexpected decode error: \(error)")
                recorder.finish(.invalidStream)
            }
        }
        let rows = try read(saved.url)
        XCTAssertFalse(saved.complete)
        XCTAssertEqual(rows.compactMap { $0["type"] as? String }, ["header", "imu", "no_fix", "source_failure", "end"])
        XCTAssertEqual(rows[1]["timestamp_ms"] as? Int, 1000)
        XCTAssertEqual(rows[1]["timestamp_provenance"] as? String, "received")
        XCTAssertNil(rows[1]["utc_ms"])
        XCTAssertEqual(rows[2]["generation"] as? Int, 7)
        XCTAssertEqual(rows[2]["source_epoch_ms"] as? Int, 500)
        XCTAssertEqual(rows[3]["source"] as? String, "imu_primary")
        XCTAssertEqual(rows[3]["reason"] as? String, "timeout")
    }

    func testFailureBeforeTheFirstSampleStillKeepsTerminalEvidence() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            recorder.sourceFailed(.init(source: .imuPrimary, cause: .timeout))
        }
        let rows = try read(saved.url)
        XCTAssertFalse(saved.complete)
        XCTAssertEqual(rows.compactMap { $0["type"] as? String }, ["header", "source_failure", "end"])
        XCTAssertEqual(rows[1]["reason"] as? String, "timeout")
    }

    func testSameTimingBasisRegressionStillEndsAsClockReset() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let saved: TrackerRecording.Saved = try await withCheckedThrowingContinuation { continuation in
            let recorder = TrackerRecording(directory: folder) { continuation.resume(with: $0) }
            recorder.append([
                .fix(sequence: 1, receivedMS: 100, latitude: 45, longitude: -76, speed: 2, provenance: .capture),
                .fix(sequence: 2, receivedMS: 101, latitude: 45, longitude: -76, speed: 2, provenance: .received),
                .fix(sequence: 3, receivedMS: 99, latitude: 45, longitude: -76, speed: 2, provenance: .capture)
            ])
            recorder.finish()
        }
        let rows = try read(saved.url)
        XCTAssertFalse(saved.complete)
        XCTAssertEqual(rows.last?["reason"] as? String, "clock_reset")
        XCTAssertEqual(rows.filter { $0["type"] as? String == "gnss" }.count, 2)
    }

    private func read(_ url: URL) throws -> [[String: Any]] {
        let lines = try String(contentsOf: url, encoding: .utf8).split(separator: "\n")
        return try lines.map {
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])
        }
    }
}
