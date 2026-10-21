import NxrsMonitorCore

/// The app and hosted native probe share the same deadline/cleanup owner.
/// The counter observes advancing decoder deliveries, not an ICE handshake.
extension NativeVideoPeer: PlaybackStartupPeer {
    public var hasDecodedVideo: Bool { frames.count > 0 }
}
