# Frame-record storage

This domain extracts the existing frame-record acceptance/flush contract. It is
not a replacement for std::fs, std::io, or a new general filesystem interface.
It preserves queue acceptance versus commitment, overload, rollback and failure
semantics. Production execution timekeeping continues to use std::time.

| Package | Supported profile and behavior |
| --- | --- |
| api | no_std frame-record contract. Uses camera data types and common error values, never a camera provider. |
| native | Linux/macOS/Windows std files and bounded std worker. Exclusive recording directories and RCAMREC1 files. |
| nuttx | Existing scalar C bridge and NuttX VFS record sink. Synchronous bounded-fixture profile, not a production filesystem deadline promise. |
| mock | Explicit in-memory scripted storage for std/WASM tests, not persistence. |

Providers declare support in their Cargo metadata and reject unsupported targets.
The NuttX target_os=none allowance serves existing core-only compile/link fixtures;
it does not provide bare-metal filesystem support without NuttX. There is no
default provider or placeholder web implementation.

The native nxrs profile requests storage through
`storage::open(...)` and injects the returned capability into the
existing recording service. The build enables `nxrs-storage/native` on the
capability facade; the app does not depend on the provider package. Factories do
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

NuttX append rolls back a failed write; failed rollback poisons the sink. The C
operation bodies and Rust state transitions are moved unchanged. This does not
establish power-loss atomicity, directory durability, or multiwriter safety.
Descriptor ownership stays in shared NuttX support; no second implementation is
introduced. Shared support consumes a descriptor even when close reports error.

Compile nuttx/ffi/storage.c and shared support with configured NuttX headers.
The simulator/QEMU recipes move write/seek/truncate/fsync fault interposition to
storage.c alongside the operations it exercises. Native structures do not cross
the scalar Rust/C ABI. Transport and core-only clock compatibility remain in the
old aggregate pending their separate migration.

## Qualification

Run python tools/check-storage-isolation.py. Each build starts with an empty
target directory and must have the exact expected compiler-artifact package set.
API/native/mock/NuttX builds must not activate camera or network providers.
The storage-only executable writes two records which Python decodes independently.
Native-on-WASM and NuttX-on-host must fail explicitly. Tests cover queue-credit
backpressure, preallocated buffer reuse, commit failure, partial writes, FIFO
flush barriers, explicit finish, and nonblocking Drop while the worker is
deliberately stalled.

The camera API dependency supplies the existing Frame schema only. This is a
frame recorder, not a claim that all future storage consumers must use camera
metadata. Generalizing that record model is outside this extraction.

These tests do not prove storage-only MCU driver exclusion, physical flash
reliability, browser storage I/O, or NuttX integration of the actual app binary.
The current NuttX coverage continues to use synthetic core-only test images.
