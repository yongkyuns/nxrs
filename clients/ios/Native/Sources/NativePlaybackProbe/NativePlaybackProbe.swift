import Foundation
import NxrsMonitorCore
import NxrsMonitorNative

/// Hosted-macOS receiver using the SAME native peer, startup owner and WHEP adapter as the app.
/// Only the test transport differs, to scope the ephemeral CA to each session.
/// Accept a private config file path, never credentials in command arguments.
@main struct NativePlaybackProbe {
    struct Input: Decodable {
        let gateway: URL
        let mediaOrigin: URL
        let credentialFile: String
        let cameraID: String
        let rootCertificate: String
        let wrongRootCertificate: String
        let leafCertificate: String
    }
    @MainActor static func main() async {
        guard CommandLine.arguments.count == 2 else {
            print("NATIVE PLAYBACK FAILED: configuration argument required")
            exit(1)
        }
        var stage = "configuration"
        var transport: ProbeTransport?
        var startup: PlaybackStartup?
        do {
            let path = URL(fileURLWithPath: CommandLine.arguments[1])
            let data = try Data(contentsOf: path)
            guard data.count < 16384 else { throw MonitorError.invalidContract }
            let input = try JSONDecoder().decode(Input.self, from: data)
            let fixture = try FixtureHTTPS(rootDER: Data(contentsOf: URL(fileURLWithPath: input.rootCertificate)),
                endpoints: [input.gateway, input.mediaOrigin])
            stage = "TLS negative controls"
            try await fixture.verifyBoundary(gateway: input.gateway,
                wrongRootDER: Data(contentsOf: URL(fileURLWithPath: input.wrongRootCertificate)),
                leafDER: Data(contentsOf: URL(fileURLWithPath: input.leafCertificate)))
            let checkedTransport = ProbeTransport(underlying: fixture)
            transport = checkedTransport
            let credential = try BearerCredential(String(contentsOfFile: input.credentialFile,
                encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines))
            let policy = try EndpointPolicy(gateway: input.gateway, additionalMediaOrigins: [input.mediaOrigin])
            let api = GatewayClient(policy: policy, transport: checkedTransport)
            let cameraID = try ResourceID(input.cameraID)
            stage = "HTTPS catalog"
            let cameras = try await api.cameras(credential: credential)
            guard cameras.contains(where: { $0.cameraID == cameraID && $0.source == .synthetic }) else {
                throw MonitorError.invalidContract
            }
            let whep = WHEPClient(policy: policy, transport: checkedTransport)
            let peer = NativeVideoPeer()
            let attempt = PlaybackStartup(peer: peer, api: api, whep: whep)
            startup = attempt
            do {
                stage = "bounded startup"
                try await attempt.start(cameraID: cameraID, credential: credential)
                // Startup has already required the first decoded frame within one budget.
                // A separate sample window verifies that this is advancing video, not one frame.
                stage = "decoded frame sample"
                let deadline = ContinuousClock.now.advanced(by: .seconds(15))
                while peer.frames.count < 5 {
                    guard !peer.failed, ContinuousClock.now < deadline else {
                        throw NativePeerError.timedOut
                    }
                    try await Task.sleep(for: .milliseconds(100))
                }
                let frames = peer.frames
                guard frames.width == 320, frames.height == 240 else { throw MonitorError.invalidContract }
                stage = "WHEP DELETE"
                guard await attempt.close() else { throw WHEPError.unavailable }
                print("NATIVE PLAYBACK PASS: \(frames.count) advancing 320x240 frames; native libWebRTC; bounded startup; DELETE completed")
            } catch {
                await attempt.close()
                throw error
            }
        } catch {
            let response = await transport?.summary ?? "no completed request"
            let startupStage = startup?.stage.rawValue ?? "not started"
            print("NATIVE PLAYBACK FAILED: \(stage) (\(diagnosticCategory(error))); startup \(startupStage); \(response)")
            exit(1)
        }
    }
}
