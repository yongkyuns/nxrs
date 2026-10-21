import Foundation
import XCTest
@testable import NxrsMonitorCore

final class ContractTests: XCTestCase {
    func testIDsAreOpaqueSafePathSegments() throws {
        XCTAssertEqual(try ResourceID("cam_123-A").value, "cam_123-A")
        for invalid in ["", "../camera", "camera/a", "10.0.0.1", "camera?x=1", "cam\n", String(repeating: "a", count: 65)] {
            XCTAssertThrowsError(try ResourceID(invalid))
        }
    }
    func testCredentialValidationAndRedaction() throws {
        let secret = "test-secret"
        let credential = try BearerCredential(secret)
        XCTAssertFalse(String(describing: credential).contains(secret))
        XCTAssertFalse(String(reflecting: credential).contains(secret))
        XCTAssertEqual(Mirror(reflecting: credential).children.count, 0)
        for invalid in ["", "Bearer token", "x\r\nInjected: value", "=", "x=y", "é", String(repeating: "a", count: 8193)] {
            XCTAssertThrowsError(try BearerCredential(invalid))
        }
        XCTAssertNoThrow(try BearerCredential("abc+/=="))
    }
    func testGatewayRejectsInsecureOrAmbiguousAddresses() throws {
        for text in ["http://home.test", "https://user:pass@home.test", "https://home.test/?token=x",
                     "https://home.test/#fragment", "https://home.test/a/../b", "https://home.test/a%2fb",
                     "https://home.test:0", "https://home.test:65536", "https://exa%20mple.test"] {
            XCTAssertThrowsError(try EndpointPolicy(gateway: URL(string: text)!))
        }
        XCTAssertNoThrow(try EndpointPolicy(gateway: URL(string: "https://[::1]:8443")!))
    }
    func testMediaOriginIsAnExplicitTrustDecision() throws {
        let policy = try EndpointPolicy(gateway: URL(string: "https://api.example.test/nxrs")!,
                                       additionalMediaOrigins: [URL(string: "https://media.example.test")!])
        XCTAssertNoThrow(try policy.validateMediaEndpoint(URL(string: "https://MEDIA.example.test:443/reference/whep")!))
        for text in ["https://evil.example.test/reference/whep", "https://media.example.test:8443/reference/whep",
                     "https://media.example.test/reference/whep?token=x", "http://media.example.test/reference/whep"] {
            XCTAssertThrowsError(try policy.validateMediaEndpoint(URL(string: text)!))
        }
        XCTAssertThrowsError(try EndpointPolicy(gateway: URL(string: "https://api.example.test")!,
                                               additionalMediaOrigins: [URL(string: "https://media.example.test/a")!]))
    }
    func testSessionLocationCannotChangeOriginEvenToAnotherEnrolledOrigin() throws {
        let endpoint = URL(string: "https://media.example.test/reference/whep")!
        let policy = try EndpointPolicy(gateway: URL(string: "https://api.example.test")!,
                                       additionalMediaOrigins: [URL(string: "https://media.example.test")!])
        XCTAssertEqual(try policy.sessionURL(location: "/reference/whep/session_1", endpoint: endpoint).path,
                       "/reference/whep/session_1")
        XCTAssertEqual(try policy.sessionURL(location: "whep/session_1", endpoint: endpoint).path,
                       "/reference/whep/session_1")
        for location in ["https://api.example.test/session", "//evil.test/session", "http://media.example.test/session",
                         "/session?token=x", "/session#fragment", " /session", "\\evil.test\\session"] {
            XCTAssertThrowsError(try policy.sessionURL(location: location, endpoint: endpoint))
        }
    }
}
