import Foundation

/// One encoded video frame's source identity and contemporaneous observations.
/// Independent of SSE receipt time and decoder clock origin.
public struct VideoDetectionFrame: Decodable, Sendable {
    public let api_version, epoch: String
    public let frame_sequence, frame_capture_ms: UInt64
    public let source: DetectionSource?
    public func validate() throws {
        guard api_version == "1", frame_sequence > 0, epoch.utf8.count == 32,
              epoch.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { throw MonitorError.invalidContract }
        if let s = source {
            try s.validate()
            guard s.emitted_ms <= frame_capture_ms || s.emitted_ms - frame_capture_ms <= 1000 else { throw MonitorError.invalidContract }
            if let o = s.latest {
                guard o.frame_sequence <= frame_sequence, o.captured_ms <= frame_capture_ms else { throw MonitorError.invalidContract }
            }
        }
    }
    public func presence(additionalAge: TimeInterval = 0) -> DetectionPresence {
        guard additionalAge.isFinite, additionalAge >= 0, let s = source, let o = s.latest,
              o.captured_ms <= frame_capture_ms,
              Double(frame_capture_ms - o.captured_ms) / 1000 + additionalAge < 5 else { return .unavailable }
        return s.presence
    }
    public var boxes: [DetectedObject] {
        guard let s = source, s.localization, s.presence == .person, let o = s.latest,
              o.captured_ms <= frame_capture_ms, frame_capture_ms - o.captured_ms < 1500 else { return [] }
        return o.objects
    }
    public var remainingLifetime: TimeInterval {
        guard !boxes.isEmpty, let o = source?.latest else { return 0 }
        return Double(1500 - (frame_capture_ms - o.captured_ms)) / 1000
    }
}

/// Standard H.264 user_data_unregistered SEI. Bounded Annex-B and AVCC parsing;
/// unrelated encoder SEI is ignored. No inference or codec implementation here.
public enum VideoDetectionSEI {
    private static let uuid = Array("nxrs-video-v1".utf8) + [0, 0, 0]
    public static func decode(_ data: Data) throws -> VideoDetectionFrame? {
        guard data.count <= 1_048_576 else { throw MonitorError.responseTooLarge }
        let b = [UInt8](data)
        var result: VideoDetectionFrame?
        func nal(_ bytes: ArraySlice<UInt8>) throws {
            guard let first = bytes.first, first & 31 == 6 else { return }
            var rbsp = [UInt8](); rbsp.reserveCapacity(min(bytes.count, 8192)); var zeros = 0
            for v in bytes.dropFirst() {
                if zeros >= 2 && v == 3 { zeros = 0; continue }
                guard rbsp.count < 8192 else { throw MonitorError.responseTooLarge }
                rbsp.append(v); zeros = v == 0 ? zeros + 1 : 0
            }
            var at = 0
            func number() throws -> Int {
                var value = 0
                while at < rbsp.count {
                    let v = Int(rbsp[at]); at += 1; value += v
                    guard value <= 8192 else { throw MonitorError.responseTooLarge }
                    if v != 255 { return value }
                }
                throw MonitorError.invalidContract
            }
            while at < rbsp.count && rbsp[at] != 128 {
                let type = try number(), size = try number()
                guard size <= rbsp.count - at else { throw MonitorError.invalidContract }
                if type == 5, size >= 16, Array(rbsp[at..<at + 16]) == uuid {
                    guard result == nil, size - 16 <= 4608 else { throw MonitorError.invalidContract }
                    let frame = try JSONDecoder().decode(VideoDetectionFrame.self, from: Data(rbsp[at + 16..<at + size]))
                    try frame.validate(); result = frame
                }
                at += size
            }
        }
        func code(_ i: Int) -> Int {
            if i + 4 <= b.count && b[i..<i + 4].elementsEqual([0, 0, 0, 1]) { return 4 }
            if i + 3 <= b.count && b[i..<i + 3].elementsEqual([0, 0, 1]) { return 3 }
            return 0
        }
        var at = 0, count = 0
        if code(0) > 0 {
            while at < b.count {
                let prefix = code(at); guard prefix > 0 else { throw MonitorError.invalidContract }
                let start = at + prefix; var end = start
                while end < b.count && code(end) == 0 { end += 1 }
                try nal(b[start..<end]); at = end; count += 1
                guard count <= 256 else { throw MonitorError.responseTooLarge }
            }
        } else {
            while at < b.count {
                guard b.count - at >= 4 else { throw MonitorError.invalidContract }
                let length = b[at..<at + 4].reduce(0) { ($0 << 8) | Int($1) }; at += 4
                guard length > 0 && length <= b.count - at else { throw MonitorError.invalidContract }
                try nal(b[at..<at + length]); at += length; count += 1
                guard count <= 256 else { throw MonitorError.responseTooLarge }
            }
        }
        return result
    }
}

public struct OverlayRectangle: Equatable, Sendable {
    public let x, y, width, height: Double
    /// Source coordinates rotate with video before aspect-fit letterboxing.
    public static func fit(_ b: DetectionBounds, width: Int, height: Int, rotation: Int,
                           viewWidth: Double, viewHeight: Double) -> Self? {
        guard width > 0, height > 0, viewWidth.isFinite, viewHeight.isFinite, viewWidth > 0, viewHeight > 0,
              [0, 90, 180, 270].contains(rotation), b.left >= 0, b.top >= 0,
              b.left < b.right, b.top < b.bottom, b.right <= 1000, b.bottom <= 1000 else { return nil }
        var l = Double(b.left) / 1000, t = Double(b.top) / 1000
        var r = Double(b.right) / 1000, bottom = Double(b.bottom) / 1000
        switch rotation {
        case 90: (l,t,r,bottom) = (1-bottom,l,1-t,r)
        case 180: (l,t,r,bottom) = (1-r,1-bottom,1-l,1-t)
        case 270: (l,t,r,bottom) = (t,1-r,bottom,1-l)
        default: break
        }
        let swapped = rotation == 90 || rotation == 270
        let w = Double(swapped ? height : width), h = Double(swapped ? width : height)
        let scale = min(viewWidth / w, viewHeight / h), dx = (viewWidth - w * scale) / 2, dy = (viewHeight - h * scale) / 2
        return Self(x: dx + l * w * scale, y: dy + t * h * scale, width: (r-l) * w * scale, height: (bottom-t) * h * scale)
    }
}
