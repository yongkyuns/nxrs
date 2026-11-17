# Matter service foundation

The gateway owns a durable identity store and one authenticated detection
subscription per monitored camera. The external Matter fixture uses both.
[Reproduction and host evidence](../tests/matter/README.md).

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
