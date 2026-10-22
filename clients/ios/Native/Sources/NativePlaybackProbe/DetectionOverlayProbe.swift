import Foundation
import NxrsMonitorCore
import NxrsMonitorNative
@preconcurrency import WebRTC

extension NativePlaybackProbe {
    @MainActor static func runDetectionOverlayProbe() async {
        var startup: PlaybackStartup?
        var stage = "configuration"
        do {
            guard let seconds = Double(CommandLine.arguments[3]), (5...900).contains(seconds) else { throw MonitorError.invalidContract }
            let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
            guard data.count < 16384 else { throw MonitorError.invalidContract }
            let input = try JSONDecoder().decode(Input.self, from: data)
            let fixture: any HTTPTransport
            if input.systemTrust == true { fixture = BoundedHTTPTransport() }
            else { fixture = try FixtureHTTPS(rootDER: Data(contentsOf: URL(fileURLWithPath: input.rootCertificate)), endpoints: [input.gateway, input.mediaOrigin]) }
            let credential = try BearerCredential(String(contentsOfFile: input.credentialFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines))
            let policy = try EndpointPolicy(gateway: input.gateway, additionalMediaOrigins: [input.mediaOrigin])
            let peer = NativeVideoPeer()
            let witness = input.verifySyntheticColors == true ? OverlayColorWitness(peer: peer) : nil
            if let witness { peer.addRenderer(witness) }
            let attempt = PlaybackStartup(peer: peer, api: GatewayClient(policy: policy, transport: fixture), whep: WHEPClient(policy: policy, transport: fixture))
            startup = attempt
            stage = "startup"
            try await attempt.start(cameraID: ResourceID(input.cameraID), credential: credential)
            stage = "sample"
            let start = ProcessInfo.processInfo.systemUptime
            let initialCount = peer.frames.count
            var count = initialCount, seen = 0, mapped = 0, boxed = 0, clear = 0
            var previous: VideoDetectionFrame?
            var observations = Set<UInt64>(), epochs = Set<String>()
            // The synthetic witness qualifies identity, not hosted-runner FPS.
            // Allow bounded extra time to collect both independent pixel controls.
            let sampleLimit = seconds + (witness == nil ? 0 : 15)
            while ProcessInfo.processInfo.systemUptime - start < sampleLimit {
                if ProcessInfo.processInfo.systemUptime - start >= seconds {
                    guard let witness else { break }
                    let stats = witness.snapshot()
                    if stats.red >= 20, stats.blue >= 20 { break }
                }
                let frame = peer.frames
                if frame.count != count {
                    count = frame.count; seen += 1
                    if let value = frame.videoMetadata {
                        mapped += 1; epochs.insert(value.epoch)
                        if let previous, previous.epoch == value.epoch {
                            guard previous.frame_sequence < value.frame_sequence, previous.frame_capture_ms <= value.frame_capture_ms else { throw MonitorError.invalidContract }
                        }
                        previous = value
                        if !value.boxes.isEmpty { boxed += 1 }
                        if value.source?.presence == .clear { clear += 1 }
                        if let o = value.source?.latest { observations.insert(o.frame_sequence) }
                    }
                }
                guard !peer.failed else { throw NativePeerError.unavailable }
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            let elapsed = ProcessInfo.processInfo.systemUptime - start
            print("OVERLAY SAMPLE decoded=\(peer.frames.count - initialCount) sampled=\(seen) mapped=\(mapped) stats=\(peer.overlayStatistics)")
            guard seen > 0, mapped * 100 >= seen * 95 else { throw MonitorError.invalidContract }
            if let witness {
                let stats = witness.snapshot()
                print("PIXEL SAMPLE red=\(stats.red) blue=\(stats.blue) mismatches=\(stats.mismatches)")
                guard stats.red >= 20, stats.blue >= 20, stats.mismatches == 0 else { throw MonitorError.invalidContract }
            } else {
                guard peer.frames.count - initialCount >= UInt64(seconds * 5) else { throw MonitorError.invalidContract }
            }
            guard await attempt.close() else { throw WHEPError.unavailable }
            var report: [String: Any] = ["result":"PASS","seconds":elapsed,"decoded_frames":peer.frames.count - initialCount,"sampled_frames":seen,"mapped_frames":mapped,
                "decoded_fps":Double(peer.frames.count - initialCount)/elapsed,"frames_with_boxes":boxed,"clear_frames":clear,"observations":observations.count,"video_epochs":epochs.count]
            if let witness { let s = witness.snapshot(); report["red_person_frames"] = s.red; report["blue_clear_frames"] = s.blue; report["pixel_identity_mismatches"] = s.mismatches }
            print(String(decoding: try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]), as: UTF8.self))
        } catch {
            if let startup { await startup.close() }
            print("{\"result\":\"FAIL\",\"stage\":\"\(stage)\",\"category\":\"\(String(reflecting: type(of: error)))\"}"); exit(1)
        }
    }
}

/// Independent pixels verify the input-identity -> encoded-AU -> decoded-buffer
/// association. Enabled only against the red/blue synthetic fixture.
private final class OverlayColorWitness: NSObject, RTCVideoRenderer, @unchecked Sendable {
    private weak var peer: NativeVideoPeer?
    private let lock = NSLock()
    private var red = 0, blue = 0, mismatches = 0
    init(peer: NativeVideoPeer) { self.peer = peer }
    func setSize(_ size: CGSize) {}
    func renderFrame(_ frame: RTCVideoFrame?) {
        guard let frame, let metadata = peer?.videoDetections(for: frame) else { return }
        let pixel = frame.buffer.toI420()
        let u = Int(pixel.dataU[0]), v = Int(pixel.dataV[0])
        let isRed = v > 180 && u < 150, isBlue = u > 180 && v < 150
        lock.lock(); defer { lock.unlock() }
        if isRed { red += 1; if metadata.boxes.isEmpty || metadata.source?.presence != .person { mismatches += 1 } }
        else if isBlue { blue += 1; if !metadata.boxes.isEmpty || metadata.source?.presence != .clear { mismatches += 1 } }
        else { mismatches += 1 }
    }
    func snapshot() -> (red: Int, blue: Int, mismatches: Int) {
        lock.lock(); defer { lock.unlock() }; return (red, blue, mismatches)
    }
}
