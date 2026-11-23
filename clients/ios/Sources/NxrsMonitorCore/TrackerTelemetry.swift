import Foundation

public struct TrackerTelemetry: Decodable {
    public enum Status: String, Decodable {
        case waiting, live, stale, fault
    }

    public struct IMU: Decodable {
        public let state: Status
        public let sequence: UInt64?
        public let acceleration: [Double]?
        public let gyro: [Double]?

        enum CodingKeys: String, CodingKey {
            case state, sequence = "seq", acceleration = "accel_mps2", gyro = "gyro_rps"
        }
    }

    public struct GNSS: Decodable {
        public let state: Status
        public let latitude: Double?
        public let longitude: Double?
        public let speed: Double?

        enum CodingKeys: String, CodingKey {
            case state, latitude = "lat_deg", longitude = "lon_deg", speed = "speed_mps"
        }
    }

    public let v: Int
    public let imu: [IMU]
    public let gnss: GNSS
}

/// One bounded NDJSON line across arbitrary stream splits and joins.
public struct TrackerFrames {
    private var pending = Data()
    public init() {}

    public enum FrameError: Error { case invalidRecord, captureOverflow }

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

public enum TrackerMeasurement: Equatable {
    case imu(index: Int, sequence: UInt32, receivedMS: UInt32, accel: [Float], gyro: [Float])
    case fix(sequence: UInt32, receivedMS: UInt32, latitude: Double, longitude: Double, speed: Float)
}

/// The authenticated byte stream combines dashboard JSON with versioned binary batches.
public struct TrackerExportFrames {
    public static let request = Data([0x4e, 0x58, 0x52, 0x53, 1])

    private var dashboard = TrackerFrames()
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
                guard pending[1] == 1 else {
                    throw TrackerFrames.FrameError.invalidRecord
                }
                if count == 0 { throw TrackerFrames.FrameError.captureOverflow }
                guard (29...528).contains(count) else { throw TrackerFrames.FrameError.invalidRecord }
                if pending.count == count + 4 {
                    try Self.decode(pending, emit: measurement)
                    pending.removeAll(keepingCapacity: true)
                }
            } else {
                guard pending[0] == 0x7b, pending.count <= 768 else {
                    throw TrackerFrames.FrameError.invalidRecord
                }
                if byte == 10 {
                    try dashboard.receive(pending, emit: snapshot)
                    pending.removeAll(keepingCapacity: true)
                }
            }
        }
    }

    private static func decode(_ bytes: Data, emit: (TrackerMeasurement) -> Void) throws {
        func word<T: FixedWidthInteger>(_ index: Int, as _: T.Type) -> T {
            bytes.withUnsafeBytes { T(littleEndian: $0.loadUnaligned(fromByteOffset: index, as: T.self)) }
        }
        var start = 4
        var batch: [TrackerMeasurement] = []
        while start < bytes.count {
            let kind = bytes[start]
            let size = kind == 3 ? 29 : 33
            guard (1...3).contains(kind), start + size <= bytes.count, batch.count < 16 else {
                throw TrackerFrames.FrameError.invalidRecord
            }
            let sequence = word(start + 1, as: UInt32.self)
            let time = word(start + 5, as: UInt32.self)
            if kind == 3 {
                let latitude = Double(bitPattern: word(start + 9, as: UInt64.self))
                let longitude = Double(bitPattern: word(start + 17, as: UInt64.self))
                let speed = Float(bitPattern: word(start + 25, as: UInt32.self))
                guard (-90...90).contains(latitude), (-180...180).contains(longitude),
                      speed.isFinite, speed >= 0 else { throw TrackerFrames.FrameError.invalidRecord }
                batch.append(.fix(sequence: sequence, receivedMS: time, latitude: latitude,
                                  longitude: longitude, speed: speed))
            } else {
                let axes = (0..<6).map { Float(bitPattern: word(start + 9 + 4 * $0, as: UInt32.self)) }
                guard axes.allSatisfy(\.isFinite) else { throw TrackerFrames.FrameError.invalidRecord }
                batch.append(.imu(index: Int(kind - 1), sequence: sequence, receivedMS: time,
                                  accel: Array(axes.prefix(3)), gyro: Array(axes.suffix(3))))
            }
            start += size
        }
        batch.forEach(emit)
    }
}
