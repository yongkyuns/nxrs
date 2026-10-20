import Foundation
import XCTest
@testable import NxrsMonitorCore

final class PlaybackLifecycleTests: XCTestCase {
    func testConnectionAloneNeverMeansLive() throws {
        var player = try PlaybackLifecycle()
        _ = try player.begin(now: 100)
        player.tick(now: 101)
        XCTAssertEqual(player.state, .connecting)
        player.tick(now: 115)
        XCTAssertEqual(player.state, .offline)
        XCTAssertNil(player.session)
    }
    func testStalledFrameIsNotPresentedAsLiveAndCanRecover() throws {
        var player = try PlaybackLifecycle()
        let id = try player.begin(now: 100)
        XCTAssertTrue(player.decodedFrame(session: id, now: 101))
        XCTAssertEqual(player.state, .live)
        player.tick(now: 102.9)
        XCTAssertEqual(player.state, .live)
        player.tick(now: 103)
        XCTAssertEqual(player.state, .stalled)
        XCTAssertTrue(player.decodedFrame(session: id, now: 104))
        XCTAssertEqual(player.state, .live)
    }
    func testSwitchingCameraRejectsAllOldSessionCallbacks() throws {
        var player = try PlaybackLifecycle()
        let old = try player.begin(now: 100)
        let current = try player.begin(now: 101)
        XCTAssertFalse(player.decodedFrame(session: old, now: 102))
        player.denied(session: old)
        player.disconnected(session: old)
        XCTAssertEqual(player.state, .connecting)
        XCTAssertEqual(player.session, current)
    }
    func testBackgroundStopAndDisconnectRetireSessions() throws {
        var player = try PlaybackLifecycle()
        let id = try player.begin(now: 1)
        player.stop()
        XCTAssertFalse(player.decodedFrame(session: id, now: 2))
        XCTAssertEqual(player.state, .idle)
        let next = try player.begin(now: 3)
        player.disconnected(session: next)
        XCTAssertEqual(player.state, .reconnecting)
        XCTAssertFalse(player.decodedFrame(session: next, now: 4))
    }
    func testAccessDeniedCannotBeRevivedByLateFrames() throws {
        var player = try PlaybackLifecycle()
        let id = try player.begin(now: 1)
        player.denied(session: id)
        XCTAssertFalse(player.decodedFrame(session: id, now: 2))
        XCTAssertEqual(player.state, .accessDenied)
    }
    func testInvalidAndBackwardClockValuesDoNotRefreshVideo() throws {
        XCTAssertThrowsError(try PlaybackLifecycle(stallAfter: .nan))
        var player = try PlaybackLifecycle()
        XCTAssertThrowsError(try player.begin(now: -1))
        let id = try player.begin(now: 10)
        XCTAssertFalse(player.decodedFrame(session: id, now: 9))
        XCTAssertFalse(player.decodedFrame(session: id, now: .infinity))
        XCTAssertTrue(player.decodedFrame(session: id, now: 11))
        player.tick(now: 13)
        XCTAssertFalse(player.decodedFrame(session: id, now: 12))
        XCTAssertThrowsError(try player.begin(now: 12))
        XCTAssertEqual(player.state, .stalled)
    }
}
