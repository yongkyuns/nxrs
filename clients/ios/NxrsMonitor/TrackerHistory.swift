import Foundation
import NxrsTrackerCore

struct TrackerHistory: Sendable {
    struct Motion: Identifiable, Sendable {
        let sequence: UInt32
        let receivedMS: UInt64
        let utcMS: Int64?
        let acceleration: [Float]
        let gyro: [Float]
        var id: UInt32 { sequence }
    }

    struct Position: Identifiable, Sendable {
        let sequence: UInt32
        let receivedMS: UInt64
        let utcMS: Int64?
        let latitude: Double
        let longitude: Double
        let speed: Float
        var id: UInt32 { sequence }
    }

    struct Count: Sendable {
        private(set) var received: UInt64 = 0
        private(set) var dropped: UInt64 = 0
        var last: UInt32?
        var firstTime: UInt64?
        var lastTime: UInt64?
        var rate: Double? {
            guard let firstTime, let lastTime else { return nil }
            let elapsed = lastTime >= firstTime ? lastTime - firstTime : UInt64(UInt32(truncatingIfNeeded: lastTime) &- UInt32(truncatingIfNeeded: firstTime))
            return elapsed >= 2_000 ? Double(received - 1) * 1_000 / Double(elapsed) : nil
        }

        mutating func accept(_ sequence: UInt32, time: UInt64) -> Bool {
            if let last {
                let step = sequence &- last
                guard step > 0, step < 0x8000_0000 else { return false }
                dropped += UInt64(step - 1)
            }
            last = sequence
            received += 1
            if firstTime == nil { firstTime = time }
            lastTime = time
            return true
        }
    }

    var motion: [[Motion]] = [[], []]
    var positions: [Position] = []
    var counts: [Count] = [.init(), .init(), .init()]
    var hasData: Bool { counts.contains { $0.received != 0 } }
    var hasGaps: Bool { counts.contains { $0.dropped != 0 } }

    mutating func append(_ measurement: TrackerMeasurement) {
        switch measurement {
        case let .imu(index, sequence, time, acceleration, gyro, utc):
            guard counts[index].accept(sequence, time: time) else { return }
            motion[index].append(Motion(sequence: sequence, receivedMS: time, utcMS: utc, acceleration: acceleration, gyro: gyro))
        case let .fix(sequence, time, latitude, longitude, speed, utc):
            guard counts[2].accept(sequence, time: time) else { return }
            positions.append(Position(sequence: sequence, receivedMS: time, utcMS: utc, latitude: latitude, longitude: longitude, speed: speed))
        }
    }

    mutating func trim() {
        for index in motion.indices where motion[index].count > 500 {
            motion[index].removeFirst(motion[index].count - 500)
        }
        if positions.count > 600 { positions.removeFirst(positions.count - 600) }
    }
}
