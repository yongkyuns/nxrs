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
        XCTAssertEqual(rows[2]["received_ms"] as? Int, 100)
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

    private func read(_ url: URL) throws -> [[String: Any]] {
        let lines = try String(contentsOf: url, encoding: .utf8).split(separator: "\n")
        return try lines.map {
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])
        }
    }
}
