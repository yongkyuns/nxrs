import Foundation

/// Trust is configured at enrollment, never expanded by a playback response.
public struct EndpointPolicy: Sendable {
    public let gateway: URL
    private let mediaOrigins: Set<Origin>

    public init(gateway: URL, additionalMediaOrigins: [URL] = []) throws {
        self.gateway = try Self.checked(gateway)
        var origins: Set<Origin> = [try Origin(gateway)]
        for url in additionalMediaOrigins {
            let checked = try Self.checked(url)
            guard checked.path.isEmpty || checked.path == "/" else {
                throw MonitorError.invalidEndpoint
            }
            origins.insert(try Origin(checked))
        }
        self.mediaOrigins = origins
    }

    public func validateMediaEndpoint(_ url: URL) throws -> URL {
        let checked = try Self.checked(url)
        guard mediaOrigins.contains(try Origin(checked)) else { throw MonitorError.untrustedOrigin }
        return checked
    }

    /// WHEP Location may be relative, but credentials cannot follow it to another origin.
    public func sessionURL(location: String, endpoint: URL) throws -> URL {
        let endpoint = try validateMediaEndpoint(endpoint)
        guard !location.isEmpty, location.utf8.allSatisfy({ $0 > 32 && $0 < 127 }),
              !location.contains("\\"), let resolved = URL(string: location, relativeTo: endpoint)?.absoluteURL
        else { throw MonitorError.invalidEndpoint }
        let checked = try Self.checked(resolved)
        guard try Origin(checked) == Origin(endpoint) else { throw MonitorError.untrustedOrigin }
        return checked
    }

    func apiURL(_ components: [String]) -> URL {
        components.reduce(gateway) { $0.appendingPathComponent($1) }
    }

    private static func checked(_ url: URL) throws -> URL {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              host.utf8.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) ||
                  (48...57).contains($0) || [45, 46, 58, 91, 93].contains($0) }),
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.port == nil || (1...65535).contains(parts.port!),
              !url.absoluteString.contains("\\"),
              url.absoluteString.utf8.allSatisfy({ $0 > 32 && $0 < 127 }),
              !parts.percentEncodedPath.contains("%"),
              !parts.path.split(separator: "/").contains(where: { $0 == "." || $0 == ".." })
        else { throw MonitorError.invalidEndpoint }
        return url
    }

    private struct Origin: Hashable, Sendable {
        let host: String
        let port: Int
        init(_ url: URL) throws {
            guard url.scheme?.lowercased() == "https", let host = url.host, !host.isEmpty else {
                throw MonitorError.invalidEndpoint
            }
            self.host = host.lowercased()
            self.port = url.port ?? 443
        }
    }
}
