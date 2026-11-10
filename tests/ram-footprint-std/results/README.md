# Recorded host witness

[`host-2026-11-10.json`](host-2026-11-10.json) contains three repetitions of the
26 fresh-process controls: **78 completed invocations** on
`x86_64-unknown-linux-gnu`, Rust 1.90.0, with a 65,536-byte worker stack request.
All payload/order, capacity, disconnect and join checks passed. Every invocation
returned requested Rust allocation bytes and blocks to its baseline. In both
service arms, the eight repeated timeout rounds made no allocator calls after
the first completed blocking timeout.

The file retains source/executable hashes, exact compiler version, capacity and
fixture reservations, per-phase allocation samples and deltas, and verified
worker totals. It omits raw output, local paths and machine identifiers. The
source was uncommitted: `base_commit` is only the ancestor, while `source_sha256`
identifies the measured content. Formatting or later changes require another
run if their hashes differ.

These are **requested Rust allocation bytes on the host**. The native heap and
stack observations are unavailable (`null`). The host stack request is neither
measured allocation nor MCU usage. This file provides no target compilation,
physical RAM, minimum-stack or timing qualification. No target result is
inferred from it.
