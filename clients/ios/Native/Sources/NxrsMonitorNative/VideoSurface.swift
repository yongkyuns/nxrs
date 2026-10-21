#if os(iOS)
import SwiftUI
@preconcurrency import WebRTC

/// Native Metal renderer, no web view. Recreate with .id(sessionID) on peer changes
/// so the previous camera's last frame cannot remain under a new camera name.
public struct VideoSurface: UIViewRepresentable {
    private let peer: NativeVideoPeer
    public init(peer: NativeVideoPeer) { self.peer = peer }
    public func makeUIView(context: Context) -> RTCMTLVideoView {
        let view = RTCMTLVideoView(frame: .zero)
        view.videoContentMode = .scaleAspectFit
        view.isAccessibilityElement = true
        view.accessibilityLabel = "Live camera video"
        peer.addRenderer(view)
        context.coordinator.peer = peer
        return view
    }
    public func updateUIView(_ view: RTCMTLVideoView, context: Context) {}
    public static func dismantleUIView(_ view: RTCMTLVideoView, coordinator: Coordinator) {
        coordinator.peer?.removeRenderer(view)
    }
    @MainActor public final class Coordinator { var peer: NativeVideoPeer? }
    public func makeCoordinator() -> Coordinator { Coordinator() }
}
#endif
