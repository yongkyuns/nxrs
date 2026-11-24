import CoreGraphics
import Foundation
import ImageIO

/// A tracker request is one visible OpenStreetMap tile.
public struct TrackerTile: Decodable, Equatable, Sendable {
    private static let side = 170
    // Match the relay's map colors. The packet contains the RGB565 palette.
    static let mapColors: [UInt32] = [
        0xfaf8f6, 0xf2e9e3, 0xd9d0c9, 0xc4b6aa,
        0xa89a90, 0x8f8b87, 0x626262, 0x333333,
        0xaad3df, 0x80abbf, 0xcdebb1, 0xaad09b,
        0xfcd6a4, 0xeca77b, 0xe4b7ba, 0xbfc3d5
    ]
    private let zoom: UInt8
    private let x: UInt32
    private let y: UInt32

    public init(from decoder: Decoder) throws {
        var coordinates = try decoder.unkeyedContainer()
        zoom = try coordinates.decode(UInt8.self)
        x = try coordinates.decode(UInt32.self)
        y = try coordinates.decode(UInt32.self)
        let limit = UInt32(1) << zoom
        guard (15...18).contains(zoom), x < limit, y < limit, coordinates.isAtEnd else {
            throw TrackerFrames.FrameError.invalidRecord
        }
    }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.urlCache = URLCache(memoryCapacity: 1 << 20, diskCapacity: 16 << 20, directory: nil)
        config.timeoutIntervalForRequest = 6
        config.timeoutIntervalForResource = 8
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.httpAdditionalHeaders = [
            "User-Agent": "NxrsHome-MotionTracker/1 (+https://github.com/yongkyuns/nxrs)"
        ]
        return URLSession(configuration: config)
    }()

    public func download() async -> Data {
        let url = URL(string: "https://tile.openstreetmap.org/\(zoom)/\(x)/\(y).png")!
        guard let (png, response) = try? await Self.session.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200,
              response.mimeType == "image/png",
              let pixels = Self.raster(png) else {
            return reply(pixels: nil)
        }
        return reply(pixels: pixels)
    }

    func reply(pixels: Data?) -> Data {
        var result = Data("NXM5".utf8)
        result.append(zoom)
        for coordinate in [x, y] {
            withUnsafeBytes(of: coordinate.bigEndian) { result.append(contentsOf: $0) }
        }
        result.append(pixels == nil ? 1 : 0)
        if let pixels { result.append(Self.packRows(pixels)) }
        return result
    }

    private static func packRows(_ pixels: Data) -> Data {
        let paletteBytes = 32
        let rowBytes = side / 2
        var lengths = Data()
        var payload = Data()
        var runs = [UInt8]()
        lengths.reserveCapacity(side)
        payload.reserveCapacity(side * rowBytes)
        runs.reserveCapacity(rowBytes)
        pixels.withUnsafeBytes { (bytes: UnsafeRawBufferPointer) in
            for row in 0..<side {
                let offset = paletteBytes + row * rowBytes
                func color(_ x: Int) -> UInt8 {
                    let pair = bytes[offset + x / 2]
                    return x.isMultiple(of: 2) ? pair >> 4 : pair & 15
                }
                runs.removeAll(keepingCapacity: true)
                var start = 0
                while start < side && runs.count < rowBytes {
                    let value = color(start)
                    var end = start + 1
                    while end < min(side, start + 16) && color(end) == value { end += 1 }
                    runs.append(UInt8(end - start - 1) << 4 | value)
                    start = end
                }
                if runs.count < rowBytes {
                    lengths.append(UInt8(runs.count))
                    payload.append(contentsOf: runs)
                } else {
                    lengths.append(UInt8(rowBytes))
                    payload.append(contentsOf: bytes[offset..<(offset + rowBytes)])
                }
            }
        }
        var result = Data(pixels.prefix(paletteBytes))
        result.append(lengths)
        result.append(payload)
        return result
    }

    static func raster(_ png: Data) -> Data? {
        guard png.count <= 256 * 1024, png.starts(with: [137, 80, 78, 71, 13, 10, 26, 10]),
              let source = CGImageSourceCreateWithData(png as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              properties[kCGImagePropertyPixelWidth] as? Int == 256,
              properties[kCGImagePropertyPixelHeight] as? Int == 256,
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
              let color = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        let side = Self.side
        var rgba = [UInt8](repeating: 0, count: side * side * 4)
        let rendered = rgba.withUnsafeMutableBytes { (bytes: UnsafeMutableRawBufferPointer) -> Bool in
            let format = CGImageAlphaInfo.noneSkipLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
            guard let context = CGContext(data: bytes.baseAddress, width: side, height: side,
                                          bitsPerComponent: 8, bytesPerRow: side * 4,
                                          space: color, bitmapInfo: format) else { return false }
            let area = CGRect(x: 0, y: 0, width: side, height: side)
            context.setFillColor(gray: 1, alpha: 1)
            context.fill(area)
            context.interpolationQuality = .high
            context.draw(image, in: area)
            return true
        }
        guard rendered else { return nil }
        return rgba.withUnsafeBytes { encodeRaster($0, side: side) }
    }

    private static func encodeRaster(_ bytes: UnsafeRawBufferPointer, side: Int) -> Data {
        let palette: [(Int, Int, Int)] = mapColors.map { color in
            let red = Int(color >> 16)
            let green = Int((color >> 8) & 255)
            let blue = Int(color & 255)
            return (red, green, blue)
        }
        var raster = Data()
        raster.reserveCapacity(32 + side * side / 2)
        for color in mapColors {
            let red = color >> 8 & 0xf800
            let green = color >> 5 & 0x07e0
            let blue = color >> 3 & 0x001f
            let rgb565 = UInt16(red | green | blue)
            raster.append(UInt8(rgb565 >> 8))
            raster.append(UInt8(truncatingIfNeeded: rgb565))
        }
        let rowBytes = side * 4
        // Restore contrast to thin labels after resizing. Match the IP relay's kernel.
        func channel(_ offset: Int, sharpen: Bool) -> Int {
            let center = Int(bytes[offset])
            guard sharpen else { return center }
            let horizontal = Int(bytes[offset - 4]) + Int(bytes[offset + 4])
            let vertical = Int(bytes[offset - rowBytes]) + Int(bytes[offset + rowBytes])
            return min(255, max(0, (12 * center - horizontal - vertical + 4) / 8))
        }
        func nearest(_ offset: Int) -> UInt8 {
            let column = (offset % rowBytes) / 4
            let sharpen = offset >= rowBytes && offset < bytes.count - rowBytes
                && column > 0 && column < side - 1
            let (r, g, b) = (channel(offset, sharpen: sharpen),
                             channel(offset + 1, sharpen: sharpen),
                             channel(offset + 2, sharpen: sharpen))
            var best = Int.max
            var index: UInt8 = 0
            for (candidate, (pr, pg, pb)) in palette.enumerated() {
                let (dr, dg, db) = (r - pr, g - pg, b - pb)
                let distance = dr * dr + dg * dg + db * db
                if distance < best {
                    best = distance
                    index = UInt8(candidate)
                }
            }
            return index
        }
        for offset in stride(from: 0, to: bytes.count, by: 8) {
            raster.append((nearest(offset) << 4) | nearest(offset + 4))
        }
        return raster
    }
}
