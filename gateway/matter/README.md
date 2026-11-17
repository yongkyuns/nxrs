# Matter service foundation

This package implements durable blob storage for [#388](https://github.com/yongkyuns/nxrs/issues/388).
It does not yet run a production Matter node. The external protocol fixture uses
the same store through the SDK's existing `KvBlobStore` contract; see
[`../tests/matter`](../tests/matter/README.md) for the reproducer and evidence.

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
