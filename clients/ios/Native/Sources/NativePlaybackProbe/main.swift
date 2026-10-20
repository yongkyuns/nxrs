import Foundation
import NxrsMonitorCore
import NxrsMonitorNative

/// Hosted-macOS test receiver using the SAME native peer and WHEP adapter as the app.
/// Accept only a private config file path, never credentials in command arguments.
@main struct NativePlaybackProbe {
    struct Input: Decodable {
        let gateway: URL
        let mediaOrigin: URL
        let credentialFile: String
        let cameraID: String
    }
    @MainActor static func main() async {
        guard CommandLine.arguments.count == 2 else {
            print("NATIVE PLAYBACK FAILED: configuration argument required")
            exit(1)
        }
        do {
            let path = URL(fileURLWithPath: CommandLine.arguments[1])
            let data = try Data(contentsOf: path)
            guard data.count < 16384 else { throw MonitorError.invalidContract }
            let input = try JSONDecoder().decode(Input.self, from: data)
            let credential = try BearerCredential(String(contentsOfFile: input.credentialFile,
                encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines))
            let policy = try EndpointPolicy(gateway: input.gateway, additionalMediaOrigins: [input.mediaOrigin])
            let api = GatewayClient(policy: policy)
            let cameraID = try ResourceID(input.cameraID)
            let cameras = try await api.cameras(credential: credential)
            guard cameras.contains(where: { $0.cameraID == cameraID && $0.source == .synthetic }) else {
                throw MonitorError.invalidContract
            }
            let whep = WHEPClient(policy: policy)
            let peer = NativeVideoPeer()
            var session: WHEPSession?
            do {
                let offer = try await peer.completeOffer()
                let grant = try await api.playbackGrant(cameraID: cameraID, credential: credential)
                let opened = try await whep.open(grant: grant, offerSDP: offer)
                session = opened
                try await peer.apply(answer: opened.answerSDP)
                let deadline = ProcessInfo.processInfo.systemUptime + 15
                while peer.frames.count < 5 {
                    guard !peer.failed, ProcessInfo.processInfo.systemUptime < deadline else {
                        throw NativePeerError.timedOut
                    }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                let frames = peer.frames
                guard frames.width == 320, frames.height == 240 else { throw MonitorError.invalidContract }
                peer.close()
                guard await whep.close(opened) else { throw WHEPError.unavailable }
                session = nil
                print("NATIVE PLAYBACK PASS: \(frames.count) advancing 320x240 frames; native libWebRTC; DELETE completed")
            } catch {
                peer.close()
                if let session = session { await whep.close(session) }
                throw error
            }
        } catch {
            // Underlying SDK/HTTP errors can contain URLs/SDP. Do not print them.
            print("NATIVE PLAYBACK FAILED: negotiation, authorization, or decoding check")
            exit(1)
        }
    }
}
