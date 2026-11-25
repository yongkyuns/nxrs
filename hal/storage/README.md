# Frame-record storage

The `Storage` trait owns frame-record acceptance and flush semantics. Camera
supplies the frame schema; storage owns its errors and I/O policy. Ordinary
files and I/O continue to use `std::fs` and `std::io` directly.

## Choose the operation explicitly

| Operation | Public API | Availability |
| --- | --- | --- |
| Queued directory recording | `recording::FileRecorder::create_new(path, StorageLimits)` | Additive `recording` Cargo feature; requires Rust std, independently of a hardware profile. |
| Read one committed record | `recording::read_record(path, max_frame_bytes)` | Same `recording` feature. |
| Synchronous append/rollback record file | `create_record_file(path, max_records, max_frame_bytes)` | Configured NuttX storage acquisition; otherwise returns `Unsupported`. |
| Scripted acceptance and failures | `MockStorage::default()` | Explicit mock profile; no filesystem I/O. |

There is no generic `open()` whose signature changes with the profile, no
profile-selected directory-recorder fallback and no root aliases for shared
software types. The synchronous record-file factory has one signature and hides
its implementation type. An unbound or mock profile never substitutes a
scripted store or queued directory recorder for that operation.

Contract-only builds remain `no_std`. Directory recording has no default feature
and uses no OS whitelist. A consumer declares the software it needs normally:

```toml
[dependencies]
nxrs-storage = { path = "../../hal/storage", features = ["recording"] }
```

Native qualification enables this feature alongside camera replay and UDP.
Services consume only the `Storage` contract. Core-only compatibility
providers remain test-local under `tests/nuttx/support`.

## Queued recording semantics

Append means bounded queue admission. The writer preallocates exactly
`queued_records + 1` payload buffers during construction: at most
`queued_records` waiting records plus one record owned by the writer. Append
reserves a queue credit and free owned buffer before copying the borrowed frame,
so saturation returns `Busy` without copying or allocating a payload. The
worker returns the queue credit on dequeue and the payload buffer only after
I/O completion or discard following a sticky failure.

`flush()` is a FIFO commit barrier. `Busy` asks the owner to retry later; success
confirms all prior accepted records committed. A worker failure remains sticky.
`finish()` closes submission, drains and joins and may block on filesystem I/O.
`Drop` closes submission without joining. Dropping without flush or finish does
not promise completion.

## Synchronous record-file semantics

The NuttX factory creates an exclusive file with mode 0600. Rust owns the file
and uses `OpenOptions`, `Write::write_all`, `Seek`, `File::set_len` and
`File::sync_all` directly. The shared writer rolls back failed appends. Failed
truncation or repositioning poisons the sink; later append/flush calls perform
no further I/O. This operation remains separate from queued directory recording.
No production storage C bridge is needed.

## Verification

```sh
cargo test --locked -p nxrs-storage --features recording
cargo run --locked -p nxrs-storage --features recording --example native_record -- /tmp/new-recording
```

The software tests run without a profile and with explicit mock/native profiles.
They retain actual committed-byte checks, exclusive creation, queue credits,
preallocated-buffer reuse, commit failure, FIFO flush barriers, explicit finish
and nonblocking Drop while a worker is deliberately stalled. Host-injected
synchronous I/O tests retain EINTR, short writes, write-zero, rollback, quota,
sync retry and sticky-poison coverage.

Host tests do not establish physical flash reliability or NuttX execution.
The older target syscall fault suite exercises `tests/nuttx/support`; its
historical evidence is not qualification of the production Rust std writer.
