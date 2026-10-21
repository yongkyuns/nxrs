import XCTest
@testable import NxrsMonitorNative

final class RTCOperationTests: XCTestCase {
    @MainActor func testOnlyFirstCallbackCompletes() async throws {
        let result = try await RTCOperation<Int>().run { finish in
            finish(.success(7))
            finish(.failure(NativePeerError.negotiationFailed))
            finish(.success(9))
        }
        XCTAssertEqual(result, 7)
    }
    @MainActor func testSDKCallbackCannotHangForever() async throws {
        do {
            let _: Int = try await RTCOperation<Int>().run(timeout: 0.02) { _ in }
            XCTFail("Expected timeout")
        } catch { XCTAssertTrue(error is NativePeerError) }
    }
    @MainActor func testCancellationBeforeStartDoesNotCallSDK() async throws {
        let task = Task { @MainActor in
            withUnsafeCurrentTask { $0?.cancel() }
            return try await RTCOperation<Int>().run { _ in XCTFail("Cancelled operation reached SDK") }
        }
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }
    @MainActor func testCancelledOperationRejectsLateCallback() async throws {
        let operation = RTCOperation<Int>()
        let task = Task { @MainActor in try await operation.run { _ in } }
        await Task.yield()
        task.cancel()
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        operation.finish(.success(1))
    }
    @MainActor func testNativeReceiveOnlyOfferAndIdempotentClose() async throws {
        let peer = NativeVideoPeer()
        defer { peer.close() }
        let offer = try await peer.completeOffer()
        XCTAssertTrue(offer.contains("m=video"))
        XCTAssertTrue(offer.contains("a=recvonly"))
        XCTAssertFalse(offer.contains("m=audio"))
        XCTAssertEqual(peer.frames.count, 0)
        peer.close()
        peer.close()
        XCTAssertTrue(peer.isClosed)
        do { _ = try await peer.completeOffer(); XCTFail("Closed peer created offer") }
        catch { XCTAssertTrue(error is NativePeerError) }
    }
}
