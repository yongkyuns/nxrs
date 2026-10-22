# Native backend qualification

The desktop replay application runs from `app/nxrs/src/main.rs`. Camera, storage,
and transport factories live in their respective HAL crates
(`src/replay`, `src/recording` and `src/udp.rs`) and return the corresponding domain contracts; they do not construct
services or run the app. The former `platform/native` executable
is removed. Existing diagnostics and physical camera drivers are unchanged.

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

The app uses `std::time::Instant` directly for elapsed time and deadlines; there
is no native clock wrapper or clock factory. The replay origin is established
after workflow startup, as before. Elapsed time is truncated to milliseconds
and saturated to u64 for the unchanged `step(now_ms)` interface. Deterministic
behavior tests supply time values rather than waiting for a real clock.

Files, sockets and resource construction stay in the selected native capability
providers. The app binary uses std for normal entry, configuration, timing and
execution. Its portable library, services and domain HAL APIs remain no_std.
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
commit barrier used by the app. `Drop` closes submission and detaches rather
than joining; it cannot report completion/errors and must not be treated as a
substitute for flush/finish.

Version-1 records have a 40-byte little-endian header followed by exactly len
payload bytes: magic `RCAMREC1` (8), width (u16), height (u16), format (u8;
Gray8=0/Rgb565=1/Jpeg=2), reserved zero (3), sequence (u64), timestamp_ms (u64),
len (u64). The reader validates metadata, input length and an allocation limit.
The telemetry service's existing 28-byte encoding is unchanged.

## Native CLI

```sh
cargo +1.90.0 run --locked -p nxrs-applications --features native --bin nxrs -- \
  input.gray 2 2 gray8 10 new-recording-directory 127.0.0.1:9000
```

The example supports packed frames up to 65536 bytes, a two-frame portable
history, a four-record worker queue, five preallocated recording payload buffers,
and input preload up to 64 MiB. It starts Recorder and Monitor against one
CameraService, processes input, drains each consumer, and flushes accepted
records. After flush succeeds, dropping storage closes submission without an
unbounded join in the app owner. Queue Busy is retried; other errors and sequence
gaps fail the command instead of reporting success.
The app no longer uses a blanket 1 ms polling sleep. Replay exposes its next
useful owner-clock poll time, and the app owner blocks on one bounded inbox until
that deadline. A 1 ms timer is used only when a storage/transport sink explicitly
returns Busy and therefore needs a bounded retry; those retries do not poll the
camera.
It requires a receiver at the supplied UDP address; no remote acknowledgement
is implied. Use a fresh output directory for each session.

`NXRS_PLATFORM=linux` selects Linux capability bindings; use `macos` or
`windows` on those operating systems. Replay, storage and sockets share ordinary
Rust implementations outside the OS directories. The Linux and macOS profiles
also enable their respective radio implementations. With no
profile, portable contracts select no hardware or mock. HAL implementations
remain modules inside their capability crates; applications depend on the
capability API rather than concrete implementation packages.

## Pipeline evidence

Run the native contract tests and the independent end-to-end decoder:

```sh
cargo +1.90.0 test --locked -p nxrs-native-integration
RUSTUP_TOOLCHAIN=1.90.0 python tests/host/test-native-runner.py
```

The second command creates a real packed input file and loopback UDP receiver,
runs the actual app binary, and independently checks persisted payloads,
metadata, sequence IDs, timestamps and telemetry checksums in Python. It does
not reuse the Rust record decoder or mock transport. Native CI executes this on Linux, macOS and Windows, while
`nxrs-native-integration` owns the cross-capability resource/fault and
small-stack tests. This runs in addition to the portable mock scenarios,
Cortex-M0 library compilation and actual Node/Chromium WASM execution.

Tests also cover gated queue saturation without timing races, fixed-pool buffer
reuse, commit/partial-write failures, FIFO flush barriers, explicit finish versus
nonblocking Drop, record quotas, existing-path protection, malformed recordings,
replay timing/restart and independently owned sockets.
A full composition test stops/restarts Recorder while Monitor continues using
the same camera, disk and UDP backend. These are host OS/backend tests, not
physical camera, embedded driver, DMA or radio qualification.
