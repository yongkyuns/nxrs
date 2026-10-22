import Foundation
import NxrsMonitorCore
@preconcurrency import WebRTC

public struct FrameSnapshot: Sendable {
    public let count: UInt64
    public let width: Int
    public let height: Int
    public let lastArrival: TimeInterval?
    public let videoMetadata: VideoDetectionFrame?
}

/// Counts advancing deliveries and retains at most one immutable decoded frame.
private final class FrameCounter: NSObject, RTCVideoRenderer, @unchecked Sendable {
    private let lock = NSLock()
    private var count: UInt64 = 0
    private var width = 0
    private var height = 0
    private var stamp: Int64?
    private var arrival: TimeInterval?
    private var latest: RTCVideoFrame?
    private var videoMetadata: VideoDetectionFrame?
    private let metadata: VideoMetadataStore
    init(metadata: VideoMetadataStore) { self.metadata = metadata; super.init() }
    func setSize(_ size: CGSize) {}
    func renderFrame(_ frame: RTCVideoFrame?) {
        guard let frame = frame, frame.width > 0, frame.height > 0 else { return }
        let annotation = metadata.metadata(frame)
        lock.lock()
        if stamp != frame.timeStampNs {
            stamp = frame.timeStampNs
            count &+= 1
            width = Int(frame.width)
            height = Int(frame.height)
            arrival = ProcessInfo.processInfo.systemUptime
            latest = frame
            videoMetadata = annotation
        }
        lock.unlock()
    }
    func snapshot() -> FrameSnapshot {
        lock.lock(); defer { lock.unlock() }
        return FrameSnapshot(count: count, width: width, height: height, lastArrival: arrival, videoMetadata: videoMetadata)
    }
    func capture() throws -> VideoFrameReference {
        lock.lock(); defer { lock.unlock() }
        guard let latest, let arrival, ProcessInfo.processInfo.systemUptime - arrival < 2 else {
            throw NativeCaptureError.noCurrentFrame
        }
        return VideoFrameReference(latest)
    }
    func clear() {
        lock.lock(); defer { lock.unlock() }
        latest = nil; videoMetadata = nil
    }
}

/// One main-actor owner per connection. No camera/microphone capture or per-frame UI task.
@MainActor public final class NativeVideoPeer: NSObject, RTCPeerConnectionDelegate {
    private static let ssl: Void = { RTCInitializeSSL() }()
    private let factory: RTCPeerConnectionFactory
    private let metadataStore: VideoMetadataStore
    private var connection: RTCPeerConnection?
    private var track: RTCVideoTrack?
    private let counter: FrameCounter
    private var renderers: [RTCVideoRenderer] = []
    public nonisolated func videoDetections(for frame: RTCVideoFrame) -> VideoDetectionFrame? { metadataStore.metadata(frame) }
    public var overlayStatistics: String { metadataStore.statistics }
    public var frames: FrameSnapshot { counter.snapshot() }
    public var failed: Bool { connection?.connectionState == .failed || connection?.iceConnectionState == .failed }
    public var isClosed: Bool { connection == nil }

    public func snapshotJPEG() async throws -> Data {
        guard connection != nil else { throw NativeCaptureError.noCurrentFrame }
        let frame = try counter.capture()
        return try await Task.detached(priority: .userInitiated) {
            try NativeFrameImage.jpeg(frame)
        }.value
    }

    public func startRecording(to url: URL) throws -> NativeClipRecorder {
        guard connection != nil, track != nil else { throw NativeCaptureError.noCurrentFrame }
        return try NativeClipRecorder(peer: self, url: url)
    }

    public override init() {
        _ = Self.ssl
        let metadata = VideoMetadataStore()
        metadataStore = metadata
        counter = FrameCounter(metadata: metadata)
        factory = RTCPeerConnectionFactory(encoderFactory: RTCDefaultVideoEncoderFactory(),
            decoderFactory: MetadataDecoderFactory(store: metadata))
        super.init()
        let config = RTCConfiguration()
        config.sdpSemantics = .unifiedPlan
        config.bundlePolicy = .maxBundle
        config.rtcpMuxPolicy = .require
        config.iceServers = [] // Reachable LAN/VPN profile; no implicit public STUN service.
        config.continualGatheringPolicy = .gatherOnce
        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        connection = factory.peerConnection(with: config, constraints: constraints, delegate: self)
        let receive = RTCRtpTransceiverInit()
        receive.direction = .recvOnly
        if connection?.addTransceiver(of: .video, init: receive) == nil { close() }
    }

    public func completeOffer() async throws -> String {
        guard let pc = connection else { throw NativePeerError.unavailable }
        let offer: RTCSessionDescription = try await RTCOperation<RTCSessionDescription>().run { finish in
            pc.offer(for: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)) { sdp, error in
                if let sdp = sdp, error == nil { finish(.success(sdp)) }
                else { finish(.failure(NativePeerError.negotiationFailed)) }
            }
        }
        try ensureOpen(pc)
        let _: Void = try await RTCOperation<Void>().run { finish in
            pc.setLocalDescription(offer) { error in
                finish(error == nil ? .success(()) : .failure(NativePeerError.negotiationFailed))
            }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + 8
        while pc.iceGatheringState != .complete {
            try ensureOpen(pc)
            guard ProcessInfo.processInfo.systemUptime < deadline else { throw NativePeerError.timedOut }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try ensureOpen(pc)
        guard let sdp = pc.localDescription?.sdp else { throw NativePeerError.negotiationFailed }
        return sdp
    }

    public func apply(answer: String) async throws {
        guard let pc = connection else { throw NativePeerError.unavailable }
        let _: Void = try await RTCOperation<Void>().run { finish in
            pc.setRemoteDescription(RTCSessionDescription(type: .answer, sdp: answer)) { error in
                finish(error == nil ? .success(()) : .failure(NativePeerError.negotiationFailed))
            }
        }
        try ensureOpen(pc)
        guard let video = pc.receivers.compactMap({ $0.track as? RTCVideoTrack }).first else {
            throw NativePeerError.negotiationFailed
        }
        track = video
        video.add(counter)
        renderers.forEach { video.add($0) }
    }

    func overlayMetadataStore() -> VideoMetadataStore { metadataStore }

    public func addRenderer(_ renderer: RTCVideoRenderer) {
        guard connection != nil, !renderers.contains(where: { $0 === renderer }) else { return }
        renderers.append(renderer)
        track?.add(renderer)
    }

    public func removeRenderer(_ renderer: RTCVideoRenderer) {
        track?.remove(renderer)
        renderers.removeAll { $0 === renderer }
    }

    public func close() {
        metadataStore.close()
        track?.remove(counter)
        renderers.forEach { track?.remove($0) }
        renderers.removeAll()
        track = nil
        counter.clear()
        connection?.close()
        connection = nil
    }

    private func ensureOpen(_ expected: RTCPeerConnection) throws {
        try Task.checkCancellation()
        guard connection === expected else { throw CancellationError() }
    }

    // Nonisolated SDK callbacks never mutate actor-owned state. The owner polls
    // native status and the locked frame snapshot. No obsolete callback can revive a peer.
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    nonisolated public func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    nonisolated public func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {}
}
