# Matter service foundation

The external Matter fixture uses the gateway’s durable identity store, detection
subscription and shared encoded video input.
[Reproduction and host evidence](../tests/matter/README.md).

For each camera, run one `media::Reader` on the owning executor. It opens a
credentialed loopback RTSP/TCP connection and forwards H.264 pictures with their
source timestamps. Each viewer subscribes through `Video`. Eight access units
are shared across viewers. A lagging viewer resumes at a new keyframe. Source
loss or replacement ends viewers of the retired source. The caller can cancel
the reader by dropping its future. Give it a strict gateway `camera_source`
credential scoped to the camera. The gateway reconciles active RTSP sessions
against its durable denials. The reader also rechecks the same path with RTSP
DESCRIBE every three seconds, allowing two seconds for a response. This check
opens no media track. Failure retires the source even if the original media
socket can still deliver video. Revoking the shared source ends every Matter
peer using it. Selective revocation of a single Matter fabric is a separate
release gate.

`occupancy::Feed::open` takes a loopback control address, a camera ID and
a private credential file. Run the subscription on the owning executor. Its
callback receives changes in confirmed presence. On silence, disconnection,
malformed input, revocation or cancellation, it withdraws the last level.
An incomplete event cannot extend freshness. Reconnection consumes the current
snapshot without replaying the source's historical transitions.

`persistence::Store` owns one writer lock and a bounded, versioned snapshot.
`initialize` requires a nonexistent absolute directory; `open` requires valid
existing state. Neither repairs corrupt state by resetting a commissioned node.
Each successful mutation syncs a new snapshot, atomically replaces the old file,
then syncs the directory. A failed mutation makes further access fail until
restart. Callers must retire network sessions before releasing the owner.

Linux and macOS use std file operations with private file/directory permissions,
exclusive ownership and link checks. Other OS bindings return unavailable;
the snapshot format remains portable. The trusted local filesystem must preserve
file locks, atomic rename and sync semantics. State is private, not encrypted;
the checksum detects corruption, not malicious replacement by the same user.

The store creates no workers. It bounds state to 512 keys, 64 KiB per value and
1 MiB total. SDK blob writes are individually atomic; SDK-wide transactions,
schema migrations, endpoint identities and fabric-removal recovery still need
qualification. Process-kill tests are not proof of power-loss durability.
