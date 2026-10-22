import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import NxrsMonitorCore

final class ActivityTests: XCTestCase {
    private let camera = try! ResourceID("cam_one")
    private let credential = try! BearerCredential("TEST_ONLY_VIEWER")
    private func payload() -> [String: Any] {
        ["api_version":"1", "camera_id":"cam_one", "events":[[
            "event_id":String(repeating:"a", count:32), "camera_id":"cam_one", "kind":"motion",
            "started_at_ms":100000, "ended_at_ms":112000, "clip_bytes":16, "thumbnail_bytes":4,
            "kept":false, "interrupted":false]], "next_cursor":NSNull(), "can_manage":false,
         "used_bytes":20, "max_bytes":33554432, "retention_seconds":259200,
         "observed_at_ms":200000, "health":["updated_at_ms":199000, "last_frame_at_ms":199500,
            "state":"monitoring", "reconnects":0, "frames_observed":100, "clips_created":1, "clips_dropped":0]]
    }
    private func client(_ transport: any HTTPTransport, media: (any HTTPTransport)? = nil) throws -> ActivityClient {
        try ActivityClient(policy: EndpointPolicy(gateway: URL(string:"https://home.example.test/nxrs")!),
            transport: transport, mediaTransport: media ?? transport)
    }
    func testHistoryUsesEnrolledOriginAndAccountAuthorization() async throws {
        let transport = StubTransport(try JSONSerialization.data(withJSONObject:payload()))
        let page = try await client(transport).page(camera:camera, credential:credential)
        XCTAssertEqual(page.events.count,1); XCTAssertTrue(page.monitoring); XCTAssertFalse(page.canManage)
        let requests = await transport.requests
        XCTAssertEqual(requests[0].url?.absoluteString, "https://home.example.test/nxrs/v1/cameras/cam_one/activity")
        XCTAssertEqual(requests[0].value(forHTTPHeaderField:"Authorization"), "Bearer TEST_ONLY_VIEWER")
        XCTAssertEqual(requests[0].value(forHTTPHeaderField:"Cache-Control"), "no-store")
    }
    func testSharedActivityFixtureHasTheSameValidatedContract() async throws {
        let transport = StubTransport(try fixture("activity"))
        let page = try await client(transport).page(camera: camera, credential: credential)
        XCTAssertEqual(page.events[0].eventID, String(repeating: "a", count: 32))
        XCTAssertTrue(page.monitoring)
    }
    func testRejectsCrossCameraDuplicatesAndUnsafeEventIDs() async throws {
        for change in 0..<5 {
            var body = payload(); var events = body["events"] as! [[String:Any]]
            switch change {
            case 0: events[0]["camera_id"] = "other"
            case 1: events += events
            case 2: events[0]["event_id"] = "../../private"
            case 3: events[0]["ended_at_ms"] = 1
            default: events[0]["clip_bytes"] = 33554433
            }
            body["events"] = events
            let transport = StubTransport(try JSONSerialization.data(withJSONObject:body))
            do { _ = try await client(transport).page(camera:camera, credential:credential); XCTFail("Invalid history accepted") }
            catch { XCTAssertEqual(error as? MonitorError, .invalidContract) }
        }
    }
    func testOldOrStoppedObserverNeverClaimsMonitoring() async throws {
        for state in ["monitoring", "stopped", "reconnecting"] {
            var body = payload(); var health = body["health"] as! [String:Any]
            health["state"] = state; health["updated_at_ms"] = 100000; body["health"] = health
            let transport = StubTransport(try JSONSerialization.data(withJSONObject:body))
            let page = try await client(transport).page(camera:camera, credential:credential)
            XCTAssertFalse(page.monitoring)
        }
    }
    func testMediaIsBoundedAndNeverUsesAResponseProvidedURL() async throws {
        let metadata = StubTransport(try JSONSerialization.data(withJSONObject:payload()))
        let media = StubTransport(Data(repeating:0, count:16), type:"application/octet-stream")
        let client = try client(metadata, media:media)
        let page = try await client.page(camera:camera, credential:credential)
        let data = try await client.media(event:page.events[0], thumbnail:false, credential:credential)
        XCTAssertEqual(data.count,16)
        let requests = await media.requests
        XCTAssertEqual(requests[0].url?.host,"home.example.test")
        XCTAssertEqual(requests[0].url?.path,"/nxrs/v1/cameras/cam_one/activity/" + String(repeating:"a",count:32) + "/clip/parts/0")
        XCTAssertNil(requests[0].url?.query)
        let short = StubTransport(Data([1]), type:"application/octet-stream")
        do { _ = try await self.client(metadata, media:short).media(event:page.events[0], thumbnail:false, credential:credential); XCTFail("Truncated clip accepted") }
        catch { XCTAssertEqual(error as? MonitorError,.invalidContract) }
    }
    func testInvalidCursorDoesNotSendCredentialsAnywhere() async throws {
        let transport = StubTransport(Data())
        do { _ = try await client(transport).page(camera:camera, before:"../admin", credential:credential); XCTFail("Unsafe cursor accepted") }
        catch { XCTAssertEqual(error as? MonitorError,.invalidIdentifier) }
        let requests = await transport.requests; XCTAssertTrue(requests.isEmpty)
    }
    func testKeptFilterUsesItsOwnScopedPageAndRejectsUnkeptResults() async throws {
        let transport = StubTransport(try fixture("activity"))
        do { _ = try await client(transport).page(camera: camera, keptOnly: true, credential: credential); XCTFail("Unkept result accepted") }
        catch { XCTAssertEqual(error as? MonitorError, .invalidContract) }
        let requests = await transport.requests
        XCTAssertEqual(requests[0].url?.path, "/nxrs/v1/cameras/cam_one/activity/kept")
    }
    func testKeepRequiresMatchingServerAcknowledgment() async throws {
        let body = payload(); let event = (body["events"] as! [[String:Any]])[0]
        let metadata = StubTransport(try JSONSerialization.data(withJSONObject:body))
        let page = try await client(metadata).page(camera:camera, credential:credential)
        let wrong = StubTransport(try JSONSerialization.data(withJSONObject:["api_version":"1", "event":event]))
        do { _ = try await client(wrong).keep(event:page.events[0], kept:true, credential:credential); XCTFail("False Keep acknowledgment accepted") }
        catch { XCTAssertEqual(error as? MonitorError,.invalidContract) }
    }
    func testDeletionIsExplicitAuthenticatedAndRequiresAnEmptySuccess() async throws {
        let metadata = StubTransport(try fixture("activity"))
        let page = try await client(metadata).page(camera: camera, credential: credential)
        let response = StubTransport(Data(), status: 204)
        try await client(response).remove(event: page.events[0], credential: credential)
        let requests = await response.requests
        XCTAssertEqual(requests[0].httpMethod, "POST")
        XCTAssertTrue(requests[0].url!.path.hasSuffix("/delete"))
        XCTAssertEqual(requests[0].httpBody, Data("{}".utf8))
        XCTAssertEqual(requests[0].value(forHTTPHeaderField: "Authorization"), "Bearer TEST_ONLY_VIEWER")
    }
    func testBusyMediaReadRetriesWithinItsBoundAndPreservesTheExactClip() async throws {
        let metadata = StubTransport(try fixture("activity"))
        let page = try await client(metadata).page(camera: camera, credential: credential)
        let busy = BusyActivityMedia()
        let bytes = try await client(metadata, media: busy).media(event: page.events[0], thumbnail: false, credential: credential)
        XCTAssertEqual(bytes.count, 16)
        let count = await busy.calls
        XCTAssertEqual(count, 2)
    }
}

private actor BusyActivityMedia: HTTPTransport {
    private(set) var calls = 0
    func send(_ request: URLRequest) async throws -> HTTPResult {
        calls += 1
        return HTTPResult(data: Data(repeating: 0, count: 16), response: HTTPURLResponse(url: request.url!,
            statusCode: calls == 1 ? 429 : 200, httpVersion: nil, headerFields: ["Content-Type":"application/octet-stream"])!)
    }
}
