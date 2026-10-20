import Foundation
import NxrsMonitorCore

/// Diagnostics for the synthetic test only. Record structural facts, never actual
/// header values, URLs, SDP, credentials or server error text.
actor ProbeTransport: HTTPTransport {
    private let underlying: any HTTPTransport
    private(set) var summary = "no completed request"
    init(underlying: any HTTPTransport) { self.underlying = underlying }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let result = try await underlying.send(request)
        let type = result.response.value(forHTTPHeaderField: "Content-Type") ?? ""
        let hasLocation = result.response.value(forHTTPHeaderField: "Location") != nil
        let text = String(data: result.data, encoding: .utf8) ?? ""
        let categories = ["no stream", "codec", "sdp", "ice", "fingerprint", "content-type", "authentication"]
        let category = categories.first(where: { text.lowercased().contains($0) }) ?? "other"
        summary = "HTTP \(result.response.statusCode), bytes \(result.data.count), SDP MIME \(type.lowercased().hasPrefix("application/sdp")), Location \(hasLocation), CRLF SDP \(text.hasPrefix("v=0\r\n")), category \(category)"
        return result
    }
}

func diagnosticCategory(_ error: Error) -> String {
    if let error = error as? WHEPError {
        switch error {
        case .invalidSDP: return "invalid offer SDP"
        case .invalidResponse: return "invalid WHEP response"
        case .unavailable: return "media unavailable"
        case .rejected(let status): return "WHEP HTTP \(status)"
        }
    }
    if let error = error as? URLError { return "URL error \(error.code.rawValue)" }
    if let error = error as? FixtureHTTPS.Failure {
        switch error {
        case .https(let code, let decision): return "URL error \(code), trust \(decision)"
        case .control(let control, let category): return "\(control): \(category)"
        case .configuration: return "fixture configuration"
        case .untrustedCertificate: return "fixture certificate rejected"
        case .regression: return "fixture assertion"
        }
    }
    return error is MonitorError ? "control contract" : "native peer or protocol"
}
