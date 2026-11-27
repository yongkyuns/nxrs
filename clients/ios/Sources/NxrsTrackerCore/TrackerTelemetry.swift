import Foundation

public struct TrackerTelemetry: Decodable, Sendable {
    public enum Status: String, Decodable, Sendable {
        case waiting, live, stale, fault
    }

    public struct IMU: Decodable, Sendable {
        public let state: Status
        public let sequence: UInt64?
        public let receivedMS: UInt64?
        public let utcMS: Int64?
        public let acceleration: [Double]?
        public let gyro: [Double]?

        enum CodingKeys: String, CodingKey {
            case receivedMS = "received_ms", utcMS = "utc_ms"
            case state, sequence = "seq", acceleration = "accel_mps2", gyro = "gyro_rps"
        }
    }

    public struct GNSS: Decodable, Sendable {
        public let state: Status
        public let sequence: UInt64?
        public let receivedMS: UInt64?
        public let utcMS: Int64?
        public let latitude: Double?
        public let longitude: Double?
        public let speed: Double?

        enum CodingKeys: String, CodingKey {
            case sequence = "seq", receivedMS = "received_ms", utcMS = "utc_ms"
            case state, latitude = "lat_deg", longitude = "lon_deg", speed = "speed_mps"
        }
    }

    public let v: Int
    public let imu: [IMU]
    public let gnss: GNSS
    public let tile: TrackerTile?
    public let exportVersion: Int?
    public let imuScaleBits: [[UInt32]?]?

    enum CodingKeys: String, CodingKey {
        case v, imu, gnss, tile, exportVersion = "export_version", imuScaleBits = "imu_scale_bits"
    }
}

/// One bounded NDJSON line across arbitrary stream splits and joins.
public struct TrackerFrames {
    private var pending = Data()
    public init() {}

    public enum FrameError: Error {
        case invalidRecord
        case captureOverflow
        case captureOverflowAt(source: UInt8, sequence: UInt32)
    }

    public mutating func receive(_ bytes: Data, emit: (TrackerTelemetry) -> Void) throws {
        for byte in bytes {
            if byte == 10 {
                let frame = try JSONDecoder().decode(TrackerTelemetry.self, from: pending)
                pending.removeAll(keepingCapacity: true)
                guard frame.v == 1, frame.imu.count == 2,
                      frame.imu.allSatisfy({ imu in
                          if imu.state == .live {
                              guard let acceleration = imu.acceleration, let gyro = imu.gyro else { return false }
                              return imu.sequence != nil && acceleration.count == 3 && gyro.count == 3 &&
                                  acceleration.allSatisfy(\.isFinite) && gyro.allSatisfy(\.isFinite)
                          }
                          return imu.sequence == nil && imu.acceleration == nil && imu.gyro == nil
                      }),
                      valid(frame.gnss)
                else { throw FrameError.invalidRecord }
                emit(frame)
            } else {
                guard pending.count < 767 else { throw FrameError.invalidRecord }
                pending.append(byte)
            }
        }
    }

    private func valid(_ fix: TrackerTelemetry.GNSS) -> Bool {
        guard fix.state == .live else {
            return fix.latitude == nil && fix.longitude == nil && fix.speed == nil
        }
        guard let latitude = fix.latitude, let longitude = fix.longitude, let speed = fix.speed else {
            return false
        }
        return (-90...90).contains(latitude) && (-180...180).contains(longitude) && speed.isFinite && speed >= 0
    }
}

public enum TrackerMeasurement: Equatable, Sendable {
    case imu(index: Int, sequence: UInt32, receivedMS: UInt64, accel: [Float], gyro: [Float], utcMS: Int64? = nil)
    case fix(sequence: UInt32, receivedMS: UInt64, latitude: Double, longitude: Double, speed: Float, utcMS: Int64? = nil)
}

/// The authenticated byte stream combines dashboard JSON with versioned binary batches.
public struct TrackerExportFrames {
    public static let request = Data([0x4e, 0x58, 0x52, 0x53, 1])
    public static let compactRequest = Data([0x4e, 0x58, 0x52, 0x53, 2])
    public static let timedRequest = Data([0x4e, 0x58, 0x52, 0x53, 3])

    private var dashboard = TrackerFrames()
    private var scales: [[Float]?] = [nil, nil]
    private var pending = Data()

    public init() {}

    public mutating func receive(
        _ bytes: Data,
        snapshot: (TrackerTelemetry) -> Void,
        measurement: (TrackerMeasurement) -> Void
    ) throws {
        for byte in bytes {
            pending.append(byte)
            if pending[0] == 0xa5 {
                guard pending.count >= 4 else { continue }
                let count = Int(pending[2]) | (Int(pending[3]) << 8)
                let version = pending[1]
                guard (1...3).contains(version) else {
                    throw TrackerFrames.FrameError.invalidRecord
                }
                if version == 1 && count == 0 { throw TrackerFrames.FrameError.captureOverflow }
                if version >= 2 && count == 5 {
                    guard pending.count >= 9 else { continue }
                    let source = pending[4]
                    guard (1...3).contains(source) else { throw TrackerFrames.FrameError.invalidRecord }
                    let sequence = pending.withUnsafeBytes {
                        UInt32(littleEndian: $0.loadUnaligned(fromByteOffset: 5, as: UInt32.self))
                    }
                    throw TrackerFrames.FrameError.captureOverflowAt(source: source, sequence: sequence)
                }
                let bounds = version == 3 ? 33...600 : 29...528
                guard bounds.contains(count) else { throw TrackerFrames.FrameError.invalidRecord }
                if pending.count == count + 4 {
                    try Self.decode(pending, version: version, scales: scales, emit: measurement)
                    pending.removeAll(keepingCapacity: true)
                }
            } else {
                guard pending[0] == 0x7b, pending.count <= 768 else {
                    throw TrackerFrames.FrameError.invalidRecord
                }
                if byte == 10 {
                    try dashboard.receive(pending) { telemetry in
                        if let bits = telemetry.imuScaleBits, bits.count == 2 {
                            scales = bits.map { source in
                                guard let source, source.count == 2 else { return nil }
                                let values = source.map(Float.init(bitPattern:))
                                return values.allSatisfy { $0.isFinite && $0 > 0 } ? values : nil
                            }
                        }
                        snapshot(telemetry)
                    }
                    pending.removeAll(keepingCapacity: true)
                }
            }
        }
    }

    private static func decode(
        _ bytes: Data,
        version: UInt8,
        scales: [[Float]?],
        emit: (TrackerMeasurement) -> Void
    ) throws {
        func word<T: FixedWidthInteger>(_ index: Int, as _: T.Type) -> T {
            bytes.withUnsafeBytes { T(littleEndian: $0.loadUnaligned(fromByteOffset: index, as: T.self)) }
        }
        let timed = version == 3
        let clockOffset = timed ? word(4, as: Int64.self) : Int64.min
        var start = timed ? 12 : 4
        var batch: [TrackerMeasurement] = []
        var previous: [(UInt32, UInt64)?] = [nil, nil, nil]
        while start < bytes.count {
            let tag = bytes[start]
            let delta = version >= 2 && tag & 0x80 != 0
            let counts = timed && tag & 0x40 != 0
            let kind = timed ? tag & 0x1f : delta ? tag & 0x7f : tag
            let header = delta ? 2 : timed ? 13 : 9
            let size = header + (kind == 3 ? 20 : counts ? 12 : 24)
            guard (1...3).contains(kind), start + size <= bytes.count, batch.count < 16,
                  kind != 3 || !counts else { throw TrackerFrames.FrameError.invalidRecord }
            let index = Int(kind - 1)
            let sequence: UInt32
            let time: UInt64
            if delta {
                guard let (priorSequence, priorTime) = previous[index] else {
                    throw TrackerFrames.FrameError.invalidRecord
                }
                sequence = priorSequence &+ 1
                let elapsed = bytes[start + 1]
                if timed {
                    let (value, overflow) = priorTime.addingReportingOverflow(UInt64(elapsed))
                    guard !overflow else { throw TrackerFrames.FrameError.invalidRecord }
                    time = value
                } else {
                    time = UInt64(UInt32(priorTime) &+ UInt32(elapsed))
                }
            } else {
                sequence = word(start + 1, as: UInt32.self)
                time = timed ? word(start + 5, as: UInt64.self) : UInt64(word(start + 5, as: UInt32.self))
            }
            previous[index] = (sequence, time)
            let utcMS: Int64?
            if clockOffset != .min, tag & 0x20 == 0 {
                guard let monotonic = Int64(exactly: time) else { throw TrackerFrames.FrameError.invalidRecord }
                let (value, overflow) = clockOffset.addingReportingOverflow(monotonic)
                guard !overflow else { throw TrackerFrames.FrameError.invalidRecord }
                utcMS = value
            } else {
                utcMS = nil
            }
            let payload = start + header
            if kind == 3 {
                let latitude = Double(bitPattern: word(payload, as: UInt64.self))
                let longitude = Double(bitPattern: word(payload + 8, as: UInt64.self))
                let speed = Float(bitPattern: word(payload + 16, as: UInt32.self))
                guard (-90...90).contains(latitude), (-180...180).contains(longitude),
                      speed.isFinite, speed >= 0 else { throw TrackerFrames.FrameError.invalidRecord }
                batch.append(.fix(sequence: sequence, receivedMS: time, latitude: latitude,
                                  longitude: longitude, speed: speed, utcMS: utcMS))
            } else {
                let axes: [Float]
                if counts {
                    guard let scale = scales[index] else { throw TrackerFrames.FrameError.invalidRecord }
                    axes = (0..<6).map { Float(word(payload + 2 * $0, as: Int16.self)) * scale[$0 / 3] }
                } else {
                    axes = (0..<6).map { Float(bitPattern: word(payload + 4 * $0, as: UInt32.self)) }
                }
                guard axes.allSatisfy(\.isFinite) else { throw TrackerFrames.FrameError.invalidRecord }
                batch.append(.imu(index: index, sequence: sequence, receivedMS: time,
                                  accel: Array(axes.prefix(3)), gyro: Array(axes.suffix(3)), utcMS: utcMS))
            }
            start += size
        }
        batch.forEach(emit)
    }
}
