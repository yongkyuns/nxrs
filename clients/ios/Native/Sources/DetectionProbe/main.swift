import Foundation
import NxrsMonitorCore

/// Qualification executable using the production SSE client and presentation
/// policy. Trust is supplied by the OS; this executable has no TLS bypass.
struct Configuration: Decodable {
    let gateway: URL
    let credentialFile: String
    let cameraID: String
}
@main struct DetectionProbe {
    @MainActor static func main() async {
        do {
            guard CommandLine.arguments.count == 3, let seconds = Double(CommandLine.arguments[2]),
                  (1...900).contains(seconds) else { throw MonitorError.invalidContract }
            let input = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
            guard input.count <= 16384 else { throw MonitorError.responseTooLarge }
            let config = try JSONDecoder().decode(Configuration.self, from: input)
            let credential = try String(contentsOfFile: config.credentialFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
            let camera = try ResourceID(config.cameraID)
            let client = DetectionClient(policy: try EndpointPolicy(gateway: config.gateway))
            let session = DetectionSession()
            session.start(client: client, cameraID: camera, credential: try BearerCredential(credential))
            var observations = Set<String>()
            var sessions = Set<String>()
            var located = 0, fresh = 0, unavailable = 0
            let started = ProcessInfo.processInfo.systemUptime
            while ProcessInfo.processInfo.systemUptime - started < seconds {
                let record = (session.presentation,session.messageCount,session.lastError)
                let state = record.0
                if let s = state.snapshot {
                    if let id = s.source_session_id { sessions.insert(id) }
                    if let o = s.source?.latest {
                        observations.insert("\(s.source_session_id ?? "none"):\(o.frame_sequence)")
                        if !o.objects.isEmpty { located += 1 }
                    }
                    guard !s.frame_synchronized else { throw MonitorError.invalidContract }
                }
                if state.presence(now: ProcessInfo.processInfo.systemUptime, videoLive: true) != .unavailable { fresh += 1 }
                else { unavailable += 1 }
                if record.2 == .unauthenticated || record.2 == .forbidden { throw MonitorError.forbidden }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            let counts = (session.messageCount,session.reconnects,session.eventGapObserved)
            session.stop()
            guard counts.0 >= 2, observations.count >= 1, fresh >= 1 else { throw MonitorError.invalidContract }
            let result: [String: Any] = ["result": "PASS", "messages": counts.0, "unique_observations": observations.count,
                "source_sessions": sessions.count, "localized_samples": located, "fresh_presentations": fresh,
                "unavailable_presentations": unavailable, "reconnects": counts.1, "event_gap_observed": counts.2,
                "seconds": ProcessInfo.processInfo.systemUptime - started, "frame_overlays_enabled": false]
            let bytes = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
            print(String(decoding: bytes, as: UTF8.self))
        } catch {
            // Deliberately omit URLs, credentials, configuration and SDK exceptions.
            print("{\"result\":\"FAIL\",\"stage\":\"native detection subscription\"}")
            exit(1)
        }
    }
}
