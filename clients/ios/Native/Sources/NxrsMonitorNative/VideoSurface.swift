#if os(iOS)
import SwiftUI
import NxrsMonitorCore
@preconcurrency import WebRTC

/// One replaceable frame and at most one queued main-thread render. The decoder
/// never waits for UI, and teardown fences late SDK callbacks.
private final class PendingVideoFrames: @unchecked Sendable {
    private let lock = NSLock()
    private var latest: RTCVideoFrame?
    private var queued = false
    private var active = true
    func offer(_ frame: RTCVideoFrame) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard active else { return false }
        latest = frame
        if queued { return false }
        queued = true; return true
    }
    func take() -> RTCVideoFrame? {
        lock.lock(); defer { lock.unlock() }
        queued = false; let frame = latest; latest = nil; return active ? frame : nil
    }
    func close() { lock.lock(); active = false; latest = nil; lock.unlock() }
}

private final class AnnotatedMetalView: UIView, RTCVideoRenderer {
    private let metal = RTCMTLVideoView(frame: .zero)
    private let boxes = CAShapeLayer()
    private let pending = PendingVideoFrames()
    private let metadata: VideoMetadataStore
    private var currentFrame: RTCVideoFrame?
    private var annotation: VideoDetectionFrame?
    private var renderedAt: TimeInterval = 0
    private var timer: Timer?
    init(metadata: VideoMetadataStore) {
        self.metadata = metadata
        super.init(frame: .zero)
        metal.videoContentMode = .scaleAspectFit
        addSubview(metal)
        boxes.fillColor = UIColor.clear.cgColor; boxes.strokeColor = UIColor.systemOrange.cgColor
        boxes.lineWidth = 2; boxes.lineJoin = .round; layer.addSublayer(boxes)
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }; self.drawBoxes()
        }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
    }
    required init?(coder: NSCoder) { nil }
    override func layoutSubviews() { super.layoutSubviews(); metal.frame = bounds; boxes.frame = bounds; drawBoxes() }
    nonisolated func setSize(_ size: CGSize) {}
    nonisolated func renderFrame(_ frame: RTCVideoFrame?) {
        guard let frame, pending.offer(frame) else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, let frame = self.pending.take() else { return }
            self.currentFrame = frame; self.annotation = self.metadata.metadata(frame)
            self.renderedAt = ProcessInfo.processInfo.systemUptime
            self.metal.renderFrame(frame); self.drawBoxes()
        }
    }
    private func drawBoxes() {
        CATransaction.begin(); CATransaction.setDisableActions(true); defer { CATransaction.commit() }
        guard let frame = currentFrame, let annotation,
              ProcessInfo.processInfo.systemUptime - renderedAt < annotation.remainingLifetime else {
            boxes.path = nil; return
        }
        let path = CGMutablePath()
        for object in annotation.boxes {
            if let r = OverlayRectangle.fit(object.bounds, width: Int(frame.width), height: Int(frame.height),
                rotation: frame.rotation.rawValue, viewWidth: bounds.width, viewHeight: bounds.height) {
                path.addRect(CGRect(x: r.x, y: r.y, width: r.width, height: r.height))
            }
        }
        boxes.path = path
    }
    func invalidate() { pending.close(); timer?.invalidate(); timer = nil; currentFrame = nil; annotation = nil; boxes.path = nil }
}

/// Video and boxes share the same frame, rotation, aspect-fit and parent zoom.
public struct VideoSurface: UIViewRepresentable {
    private let peer: NativeVideoPeer
    public init(peer: NativeVideoPeer) { self.peer = peer }
    public func makeUIView(context: Context) -> UIView {
        let view = AnnotatedMetalView(metadata: peer.overlayMetadataStore())
        view.isAccessibilityElement = true; view.accessibilityLabel = "Live camera video"
        peer.addRenderer(view); context.coordinator.peer = peer
        return view
    }
    public func updateUIView(_ view: UIView, context: Context) {}
    public static func dismantleUIView(_ view: UIView, coordinator: Coordinator) {
        guard let view = view as? AnnotatedMetalView else { return }
        view.invalidate(); coordinator.peer?.removeRenderer(view)
    }
    @MainActor public final class Coordinator { var peer: NativeVideoPeer? }
    public func makeCoordinator() -> Coordinator { Coordinator() }
}
#endif
