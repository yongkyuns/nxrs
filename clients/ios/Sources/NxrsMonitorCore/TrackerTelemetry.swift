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

    public enum FrameError: Error { case invalidRecord }

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
