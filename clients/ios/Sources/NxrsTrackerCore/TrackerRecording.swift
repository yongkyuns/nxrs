import Foundation

/// Writes a local archive of the decoded export. The caller can enqueue
/// from its receive queue without performing file I/O there.
public final class TrackerRecording: @unchecked Sendable {
    public enum Interruption: String, Sendable {
        case reconnecting, backgrounded
    }

    public enum End: String, Sendable {
        case stopped, disconnected, invalidStream = "invalid_stream"
        case writerOverrun = "writer_overrun", clockReset = "clock_reset"
    }

    public enum WriteError: Error, Equatable, Sendable {
        case noSamples
    }

    public struct Saved: Sendable {
        public let url: URL
        public let complete: Bool
    }

    private struct Row: Encodable {
        var type: String
        var format: String?
        var version: Int?
        var source: String?
        var sequence: UInt32?
        var received_ms: UInt64?
        var utc_ms: Int64?
        var missed_before: UInt32?
        var accel_mps2: [Float]?
        var gyro_rps: [Float]?
        var lat_deg: Double?
        var lon_deg: Double?
        var speed_mps: Float?
        var reason: String?
        var complete: Bool?
    }

    private static let format = "nxrs-motion-tracker"
    private static let names = ["imu_primary", "imu_secondary", "gnss"]
    private static let capacity = 1_024
    private let queue = DispatchQueue(label: "org.nxrs.tracker.recording", qos: .utility)
    private let lock = NSLock()
    private var queued = 0
    private var accepting = true

    // Only the writer queue accesses the file and sequence state.
    private let encoder = JSONEncoder()
    private let destination: URL
    private let working: URL
    private let completed: @Sendable (Result<Saved, Error>) -> Void
    private var handle: FileHandle?
    private var lastSequence: [UInt32?] = [nil, nil, nil]
    private var lastTime: [UInt64?] = [nil, nil, nil]
    private var hasSamples = false
    private var interrupted = false
    private var hasLoss = false

    public init(
        directory: URL,
        completed: @escaping @Sendable (Result<Saved, Error>) -> Void
    ) {
        self.completed = completed
        let id = UUID().uuidString
        let date = ISO8601DateFormatter().string(from: .now).replacingOccurrences(of: ":", with: "-")
        destination = directory.appendingPathComponent("tracker-\(date)-\(id.prefix(8)).ndjson")
        working = directory.appendingPathComponent(".\(id).partial")
        queue.async {
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                try Data().write(to: self.working, options: .withoutOverwriting)
                self.handle = try FileHandle(forWritingTo: self.working)
                try self.write([Row(type: "header", format: Self.format, version: 1)])
            } catch {
                self.fail(error)
            }
        }
    }

    public static func directory() throws -> URL {
        try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                    appropriateFor: nil, create: false)
            .appendingPathComponent("Motion Tracker", isDirectory: true)
    }

    public static func files(in directory: URL) throws -> [URL] {
        try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil,
                                                    options: .skipsHiddenFiles)
            .filter { $0.pathExtension == "ndjson" }
            .sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    public func append(_ measurements: [TrackerMeasurement]) {
        guard !measurements.isEmpty else { return }
        submit(measurements.count) {
            var rows = [Row]()
            for measurement in measurements {
                let index: Int
                var row: Row
                switch measurement {
                case let .imu(source, sequence, time, acceleration, gyro, utc):
                    guard (0..<2).contains(source) else { throw CocoaError(.fileWriteUnknown) }
                    index = source
                    row = Row(type: "imu", source: Self.names[index], sequence: sequence, received_ms: time,
                              utc_ms: utc, accel_mps2: acceleration, gyro_rps: gyro)
                case let .fix(sequence, time, latitude, longitude, speed, utc):
                    index = 2
                    row = Row(type: "gnss", source: Self.names[index], sequence: sequence, received_ms: time,
                              utc_ms: utc, lat_deg: latitude, lon_deg: longitude, speed_mps: speed)
                }
                guard let time = row.received_ms, let sequence = row.sequence else { continue }
                if let previous = self.lastSequence[index] {
                    let step = sequence &- previous
                    guard step > 0, step < 0x8000_0000 else { continue }
                    if step > 1 {
                        self.hasLoss = true
                        row.missed_before = step - 1
                    }
                }
                if let previous = self.lastTime[index], time < previous {
                    try self.write(rows)
                    self.end(.clockReset)
                    return
                }
                self.lastSequence[index] = sequence
                self.lastTime[index] = time
                self.hasSamples = true
                rows.append(row)
            }
            try self.write(rows)
        }
    }

    public func interrupt(_ reason: Interruption) {
        submit(1) {
            self.interrupted = true
            try self.write([Row(type: "interruption", reason: reason.rawValue)])
        }
    }

    public func resume() {
        submit(1) {
            self.lastSequence = [nil, nil, nil]
            try self.write([Row(type: "resume")])
        }
    }

    public func finish(_ reason: End = .stopped) {
        lock.withLock {
            guard accepting else { return }
            accepting = false
            queue.async { self.end(reason) }
        }
    }

    private func submit(_ count: Int, work: @escaping @Sendable () throws -> Void) {
        lock.withLock {
            guard accepting else { return }
            guard count <= Self.capacity - queued else {
                accepting = false
                queue.async { self.end(.writerOverrun) }
                return
            }
            queued += count
            queue.async {
                defer { self.lock.withLock { self.queued -= count } }
                guard self.handle != nil else { return }
                do { try work() } catch { self.fail(error) }
            }
        }
    }

    private func write(_ rows: [Row]) throws {
        guard !rows.isEmpty, let handle else { return }
        var bytes = Data()
        for row in rows {
            try bytes.append(encoder.encode(row))
            bytes.append(10)
        }
        try handle.write(contentsOf: bytes)
    }

    private func end(_ reason: End) {
        guard let handle else { return }
        lock.withLock { accepting = false }
        guard hasSamples else {
            fail(WriteError.noSamples)
            return
        }
        let complete = reason == .stopped && !interrupted && !hasLoss
        do {
            try write([Row(type: "end", reason: reason.rawValue, complete: complete)])
            try handle.close()
            try FileManager.default.moveItem(at: working, to: destination)
            self.handle = nil
            completed(.success(Saved(url: destination, complete: complete)))
        } catch {
            fail(error)
        }
    }

    private func fail(_ error: Error) {
        lock.withLock { accepting = false }
        try? handle?.close()
        handle = nil
        try? FileManager.default.removeItem(at: working)
        completed(.failure(error))
    }
}
