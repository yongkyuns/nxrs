# Frame-record storage

This domain extracts the existing frame-record acceptance/flush contract. It is
not a replacement for std::fs, std::io, or a new general filesystem interface.
It preserves queue acceptance versus commitment, overload, rollback and failure
semantics. Production execution timekeeping continues to use std::time.

The storage API owns `storage::DeviceError`. It reuses camera frame types, but
storage failures are independent of `camera::DeviceError`.

| Provider | Supported profile and behavior |
| --- | --- |
| native | Linux/macOS/Windows std files and bounded std worker. Exclusive recording directories and RCAMREC1 files. |
| nuttx | Rust std file acquisition and shared append/rollback implementation. Synchronous bounded-fixture profile, not a production filesystem deadline promise. |
| mock | Explicit in-memory scripted storage for std/WASM tests, not persistence. |

The single `nxrs-storage` crate declares supported providers in Cargo metadata.
Set `NXRS_PLATFORM` to a checked-in profile to select one; unset builds select
`none`. Providers reject unsupported targets.
Core-only compatibility providers live under `tests/nuttx/support`; production
NuttX storage requires Rust std. There is no
default provider or placeholder web implementation.

The native nxrs profile requests storage through
`storage::open(...)` and injects the returned capability into the
existing recording service. The profile owns provider selection; the app does
not depend on provider packages. Factories do
not construct services or drive the app.

## Preserved semantics

Native append means bounded queue admission, not durable commitment. Flush uses
the existing FIFO barrier, returning Busy until acknowledged; worker failure is
sticky.

The native worker now preallocates exactly `queued_records + 1` payload buffers
during construction: at most `queued_records` waiting records plus one record
owned by the writer. Append reserves a queue credit and a free owned buffer
before copying the borrowed frame, so a saturated queue returns Busy without a
payload copy or allocation. The worker returns the queue credit when it dequeues
the request and returns the payload buffer only after I/O completion/discard.

`finish()` explicitly closes submission, drains, and joins and may block on OS
filesystem I/O. `Drop` closes submission but never joins; after a successful
flush all accepted records are already committed, so the normal app can drop
without hiding an unbounded filesystem wait in its owner loop. Dropping without
flush/finish does not promise completion.

NuttX storage uses `OpenOptions`, `Write::write_all`, `Seek`, `File::set_len`,
and `File::sync_all` directly. The shared Rust writer rolls back a failed append;
failed truncation or repositioning poisons the sink, and subsequent append/flush
calls perform no further I/O. Resource acquisition remains provider-local and
creates an exclusive file with mode 0600. No production storage C bridge remains.

Host-injected Rust I/O tests cover EINTR, short writes, write-zero, rollback,
quota preservation, sync retry and sticky poison. The older target syscall
fault suite now exercises only `tests/nuttx/support`; its historical evidence
must not be presented as qualification of the new Rust std writer.

## Qualification

Run the standalone fixture with `NXRS_PLATFORM=native cargo run -p nxrs-storage --example native_record`.
The shared isolation tooling is being updated for the consolidated crate layout.
Tests cover queue-credit
backpressure, preallocated buffer reuse, commit failure, partial writes, FIFO
flush barriers, explicit finish, and nonblocking Drop while the worker is
deliberately stalled.

The camera API dependency supplies the existing Frame schema only. This is a
frame recorder, not a claim that all future storage consumers must use camera
metadata. Generalizing that record model is outside this extraction.

These tests do not prove storage-only MCU driver exclusion, physical flash
reliability, browser storage I/O, or NuttX integration of the actual app binary.
The historical target fault coverage applies to the test-local compatibility
adapter. The new std writer still requires NuttX target execution qualification.
