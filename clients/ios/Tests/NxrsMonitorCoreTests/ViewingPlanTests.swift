import XCTest
@testable import NxrsMonitorCore

final class ViewingPlanTests: XCTestCase {
    private struct Case: Decodable {
        let name: String
        let budget: Int
        let reserved: Int?
        let requested, active, retiring, blocked, start, stop: [String]
    }
    func testSharedAdmissionAndRetirementCases() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "camera-sessions", withExtension: "json", subdirectory: "Fixtures"))
        let cases = try JSONDecoder().decode([Case].self, from: Data(contentsOf: url))
        for item in cases {
            let ids: ([String]) throws -> [ResourceID] = { try $0.map(ResourceID.init) }
            let plan = try ViewingPlan(requested: ids(item.requested), active: Set(ids(item.active)),
                retiring: Set(ids(item.retiring)), blocked: Set(ids(item.blocked)), reservedSlots: item.reserved ?? 0, maximumActive: item.budget)
            XCTAssertEqual(plan.start.map(\.value), item.start, item.name)
            XCTAssertEqual(plan.stop.map(\.value), item.stop, item.name)
        }
    }
    func testAdmissionCannotExceedBounds() throws {
        XCTAssertThrowsError(try ViewingPlan(requested: [], active: [], retiring: [], maximumActive: 0))
        XCTAssertThrowsError(try ViewingPlan(requested: [], active: [], retiring: [], maximumActive: 5))
        let ids = try (0..<65).map { try ResourceID("camera_\($0)") }
        XCTAssertThrowsError(try ViewingPlan(requested: ids, active: [], retiring: []))
    }
}
