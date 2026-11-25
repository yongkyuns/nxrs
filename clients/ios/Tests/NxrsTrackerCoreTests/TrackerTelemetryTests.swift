import CoreGraphics
import ImageIO
import Foundation
import XCTest
@testable import NxrsTrackerCore

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
    func testMapRequestAndNorthUpRaster() throws {
        let tile = try JSONDecoder().decode(TrackerTile.self, from: Data("[15,10,20]".utf8))
        XCTAssertThrowsError(try JSONDecoder().decode(TrackerTile.self, from: Data("[15,32768,20]".utf8)))
        XCTAssertEqual(tile.reply(pixels: nil), Data([78, 88, 77, 53, 15, 0, 0, 0, 10, 0, 0, 0, 20, 1]))
        let detail = try JSONDecoder().decode(TrackerTile.self, from: Data("[18,196606,262143]".utf8))
        XCTAssertEqual(detail.reply(pixels: nil), Data([78, 88, 77, 53, 18, 0, 2, 255, 254, 0, 3, 255, 255, 1]))
        XCTAssertThrowsError(try JSONDecoder().decode(TrackerTile.self, from: Data("[18,262144,20]".utf8)))
        XCTAssertThrowsError(try JSONDecoder().decode(TrackerTile.self, from: Data("[19,10,20]".utf8)))
        let source = Data(Array(repeating: [UInt8(236), 167, 123, 255], count: 256 * 128).flatMap { $0 }
                        + Array(repeating: [UInt8(170), 211, 223, 255], count: 256 * 128).flatMap { $0 })
        let image = try XCTUnwrap(CGImage(width: 256, height: 256, bitsPerComponent: 8, bitsPerPixel: 32,
                                          bytesPerRow: 1024, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                          bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
                                          provider: CGDataProvider(data: source as CFData)!, decode: nil,
                                          shouldInterpolate: false, intent: .defaultIntent))
        let encoded = NSMutableData()
        let png = try XCTUnwrap(CGImageDestinationCreateWithData(encoded, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(png, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(png))
        let pixels = try XCTUnwrap(TrackerTile.raster(encoded as Data))
        XCTAssertEqual(pixels.count, 14482)
        XCTAssertEqual(pixels[32], 0xdd)
        XCTAssertEqual(pixels.last, 0x88)
        let packet = tile.reply(pixels: pixels)
        XCTAssertLessThan(packet.count, 2500)
        XCTAssertEqual(packet[14..<46], pixels.prefix(32))
        let scanlines = packet[46..<216]
        var offset = 216
        for (row, length) in scanlines.enumerated() {
            let bytes = packet[offset..<(offset + Int(length))]
            let original = pixels[(32 + row * 85)..<(32 + (row + 1) * 85)]
            let decoded = bytes.flatMap { Array(repeating: $0 & 15, count: Int($0 >> 4) + 1) }
            XCTAssertEqual(decoded, original.flatMap { [$0 >> 4, $0 & 15] })
            offset += Int(length)
        }
        XCTAssertEqual(packet.count, offset)
    }
}

extension TrackerTelemetryTests {
    func testTimedBatchDecodesCountsAndBothClocks() throws {
        let scale = [Float(0.125).bitPattern, Float(0.25).bitPattern]
        let advertisement = """
        {"v":1,"imu_scale_bits":[[\(scale[0]),\(scale[1])],null],"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"waiting"}}
        """
        let first = UInt64(UInt32.max)
        let utc: Int64 = 1_767_225_600_000
        var payload = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            var word = value.littleEndian
            withUnsafeBytes(of: &word) { payload.append(contentsOf: $0) }
        }
        append(utc - Int64(first + 3))
        payload.append(0x61)
        append(UInt32(1))
        append(first)
        let counts: [Int16] = [.min, 0, .max, -1, 4, .max]
        counts.forEach(append)
        payload.append(3)
        append(UInt32(4))
        append(first + 3)
        append(Double(45).bitPattern)
        append(Double(-75).bitPattern)
        append(Float(2).bitPattern)
        payload.append(contentsOf: [0xc1, 9])
        counts.forEach(append)
        var parser = TrackerExportFrames()
        try parser.receive(Data((advertisement + "\n").utf8), snapshot: { _ in }, measurement: { _ in XCTFail() })
        let frame = Data([0xa5, 3, UInt8(payload.count), 0]) + payload
        var actual: [TrackerMeasurement] = []
        try parser.receive(frame.prefix(21), snapshot: { _ in XCTFail() }, measurement: { _ in XCTFail() })
        try parser.receive(Data(frame.dropFirst(21)), snapshot: { _ in XCTFail() }, measurement: { actual.append($0) })
        let accel: [Float] = [-4096, 0, 4095.875]
        let gyro: [Float] = [-0.25, 1, 8191.75]
        XCTAssertEqual(actual, [
            .imu(index: 0, sequence: 1, receivedMS: first, accel: accel, gyro: gyro),
            .fix(sequence: 4, receivedMS: first + 3, latitude: 45, longitude: -75, speed: 2, utcMS: utc),
            .imu(index: 0, sequence: 2, receivedMS: first + 9, accel: accel, gyro: gyro, utcMS: utc + 6)
        ])
    }

    func testCompactBatchRestoresSequencesAndTimestampsAcrossWrap() throws {
        let payload = [UInt8](repeating: 0, count: 24)
        let first: [UInt8] = [1, 255, 255, 255, 255, 250, 255, 255, 255]
        var parser = TrackerExportFrames()
        var samples: [TrackerMeasurement] = []
        try parser.receive(Data([0xa5, 2, 59, 0] + first + payload),
                           snapshot: { _ in XCTFail() }, measurement: { _ in XCTFail() })
        try parser.receive(Data([0x81, 11] + payload),
                           snapshot: { _ in XCTFail() }, measurement: { samples.append($0) })
        XCTAssertEqual(samples, [
            .imu(index: 0, sequence: .max, receivedMS: UInt64(UInt32.max) - 5, accel: [0, 0, 0], gyro: [0, 0, 0]),
            .imu(index: 0, sequence: 0, receivedMS: 5, accel: [0, 0, 0], gyro: [0, 0, 0])
        ])
        let orphan = Data([0xa5, 2, 52, 0] + [0x81, 1] + payload + [0x81, 1] + payload)
        XCTAssertThrowsError(try parser.receive(orphan, snapshot: { _ in XCTFail() },
                                                measurement: { _ in XCTFail() }))
    }

    func testCompactOverflowReportsFirstMissingSourceAndSequence() throws {
        var parser = TrackerExportFrames()
        try parser.receive(Data([0xa5, 2, 5, 0, 2]), snapshot: { _ in XCTFail() },
                           measurement: { _ in XCTFail() })
        XCTAssertThrowsError(try parser.receive(Data([0x20, 0x03, 0, 0]),
                                                snapshot: { _ in XCTFail() },
                                                measurement: { _ in XCTFail() })) { error in
            guard case TrackerFrames.FrameError.captureOverflowAt(source: 2, sequence: 800) = error else {
                return XCTFail("Expected the first missing module IMU sample")
            }
        }
    }

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
        {"v":1,"imu":[{"state":"waiting"},{"state":"waiting"}],"gnss":{"state":"waiting"},"tile":[18,130983,87169]}
        """.utf8) + Data([10])

        var parser = TrackerExportFrames()
        var samples: [TrackerMeasurement] = []
        var snapshots = 0
        try parser.receive(binary.prefix(16), snapshot: { _ in XCTFail() }, measurement: { _ in XCTFail() })
        try parser.receive(Data(binary.dropFirst(16)) + snapshot, snapshot: { record in
            snapshots += 1
            XCTAssertEqual(record.tile?.reply(pixels: nil), Data([78, 88, 77, 53, 18, 0, 1, 255, 167, 0, 1, 84, 129, 1]))
        },
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

    func testExportDetectsTrackerBufferOverflow() throws {
        var parser = TrackerExportFrames()
        try parser.receive(Data([0xa5, 1, 0]), snapshot: { _ in XCTFail() }, measurement: { _ in XCTFail() })
        XCTAssertThrowsError(try parser.receive(Data([0]), snapshot: { _ in XCTFail() },
                                                measurement: { _ in XCTFail() })) { error in
            guard case TrackerFrames.FrameError.captureOverflow = error else {
                return XCTFail("Expected an explicit capture overflow")
            }
        }
    }
}
