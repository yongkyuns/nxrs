# Native backend qualification

`tests/native` qualifies camera replay, directory recording and UDP using their
explicit HAL APIs. The small composition helper in `tests/camera-support` is
shared with portable and NuttX qualification; production code cannot depend on
it. The former `app/nxrs` replay CLI is retired. `app/home-cam` owns product-level
camera integration and its tests.

## Implemented adapters

- `ReplayCamera` reads a bounded packed Gray8/RGB565 file during setup. After
  setup it performs bounded in-memory capture with a configurable virtual frame
  period. Timestamps describe the replay schedule, not original exposure time.
  EOF stays pending; stop/start restarts the input. This is not a live camera.
- `FileRecorder` owns a bounded submission queue and a separate filesystem
  worker. `append` success means local QUEUE acceptance, not a committed record.
  `flush` is a nonblocking FIFO barrier: Busy requires retry, success confirms
  that all prior accepted records were committed. Async commit errors are
  sticky and reported by flush/finish; later queued records are discarded and
  never falsely reported as persisted. Recovery starts a new session.
- `UdpTransport` owns a nonblocking socket and sends one datagram per summary.
  Bind/connect use numeric addresses. Success is local submission only.

Replay, recording and UDP are additive Cargo features, independent of hardware
profile admission. Qualification enables them directly; core-only test support
enables none. Tests supply explicit camera times instead of wall-clock sleeps.

The filesystem worker boundary copies borrowed payloads into a startup-allocated
owned buffer only after it has reserved queue capacity. For queue capacity N,
exactly N+1 payload buffers exist: N may wait while one is owned by the writer.
A full queue therefore returns Busy before payload copy or allocation, and each
completed/discarded record returns its buffer to the pool. This is not a
zero-copy/DMA pipeline. Accepted-record quota bounds per-session disk growth.
The portable camera history remains separate.

## Recording commit semantics

A session is a newly created exclusive directory; existing paths are refused.
One `.rcam` file stores each record. The worker writes an exclusive `.part`
file, synchronizes its data, closes it, then publishes a non-replacing hard
link to the committed filename. Existing committed files are never replaced.
Failures before publication do not create a committed record. Failure to clean
up a temporary link after publication does not retract or duplicate acceptance.
Readers enumerate only `.rcam` files; interrupted sessions can retain `.part`.

This requires a trusted local filesystem with hard-link support. It does NOT
claim transactional behavior under remote filesystem failures, crash recovery,
power-loss directory durability, protection from another local writer modifying
the directory, or payload corruption detection. File sync is not directory sync.
Workers cannot forcibly cancel a stuck OS filesystem call. Explicit `finish()`
closes submission, drains, and joins, so it may block and should only be used
where waiting for OS I/O is acceptable. `flush()` remains the nonblocking
commit barrier available to consumers. `Drop` closes submission and detaches rather
than joining; it cannot report completion/errors and must not be treated as a
substitute for flush/finish.

Version-1 records have a 40-byte little-endian header followed by exactly len
payload bytes: magic `RCAMREC1` (8), width (u16), height (u16), format (u8;
Gray8=0/Rgb565=1/Jpeg=2), reserved zero (3), sequence (u64), timestamp_ms (u64),
len (u64). The reader validates metadata, input length and an allocation limit.
The telemetry service's existing 28-byte encoding is unchanged.

## Pipeline evidence

Run all native tests under unset, mock and host profiles:

```sh
RUSTUP_TOOLCHAIN=1.90.0 python3 tests/native/qualify.py
```

For one profile, use `cargo test --locked -p nxrs-native-integration` directly.
The composition test reads a real packed file and independently checks raw
record headers, payloads, sequence IDs, timestamps and actual loopback UDP
checksums without the production record decoder. It stops/restarts recording
while telemetry continues. Small-stack tests retain heap and borrowed-buffer
coverage. CI runs these on Linux, macOS and Windows, alongside portable
scenarios, core-only compilation and Node/Chromium WASM execution.

Tests also cover gated queue saturation without timing races, fixed-pool buffer
reuse, commit/partial-write failures, FIFO flush barriers, explicit finish versus
nonblocking Drop, record quotas, existing-path protection, malformed recordings,
replay timing/restart and independently owned sockets.
These are host OS/backend tests, not physical camera, embedded driver, DMA or
radio qualification.
