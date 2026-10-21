# NuttX storage failure qualification

The legacy fixture `FileStorage`, `RecordingService` and `Recorder` have a shared
failure suite in the NuttX sim and ESP32-S3 QEMU pipelines. The suite lives in
`tests/nuttx/src/qualification/storage.rs`, `tests/nuttx/c/storage_fault.c`
and `tests/host/nuttx_storage_checks.py`. It is not a new storage backend.

The test build interposes only the test-local C bridge's write, seek, truncate,
flush and file-create symbols. Successful calls still operate on real NuttX
VFS/tmpfs files. Injection is scoped by owning pthread and exact descriptor;
other descriptors and other threads pass through. Fault state is not compiled
into production applications/services or ordinary builds of the C bridge.
This suite preserves the legacy adapter for comparison. Production NuttX
storage now uses ordinary Rust std files and the shared Rust writer under
`hal/storage/src/platform/file.rs`; host-injected Rust tests exercise its
rollback and sticky-poison behavior. This historical target suite does not
qualify that new path. The existing close-ownership fixture remains independent.

The ordinary-std integration entry now also runs
`tests/nuttx/src/qualification/std_storage.rs`. It exercises the production Rust
writer's exclusive create, append, sync, independently decoded records, and the
std truncate/seek facilities needed for rollback. The new probe needs target
execution; its presence is not a claim that this change has already passed
NuttX runtime or physical-storage qualification.

## Cases

Each case first commits record 1, then exercises record 2 with a two-record
capacity. Eight cases cover EINTR plus short writes; ENOSPC inside the header;
EIO inside the payload; a zero-progress write; initial SEEK_END failure;
failed rollback truncation; failed rollback repositioning; and an fsync error
followed by a successful flush retry.

Recoverable append errors must preserve every byte of record 1 and restore the
writer offset. They must not advance Recorder's cursor or consume FileStorage
capacity. Retry must accept exactly record 2, and a further step must be idle.
The flush case leaves Recorder in Stopping: new frames and restart are rejected
until flush succeeds, without rewriting an already accepted record.

Failed rollback is different. FileStorage must permanently reject further
append/flush operations without making more storage calls. If truncation fails,
the test explicitly observes the incomplete record left in the file; it does
NOT claim rollback succeeded. If only repositioning fails, the file is shorter
than the writer offset, and the adapter must still remain poisoned. Both cases
must be escalated rather than retried as recoverable or reported as clean stop.

## Evidence and negative controls

A C checker reopens every actual file and checks its full bytes, EOF and writer
offset before and after retry. Syscall counters witness the injected point,
real partial progress, rollback, flush retries, and no operations after poison.
A separate Python oracle constructs expected records independently, including
64-bit timestamps, and requires exact byte/counter evidence for all eight cases.
Missing, duplicate and corrupt transcripts are rejected.

After the Rust object is dropped, C verifies that its descriptor is closed.
It then corrupts the actual file, requires its byte verifier to reject that
corruption, restores the byte, verifies again, and removes the file. Each
same-kernel invocation repeats all eight cases, so leftover state or files
cannot be hidden by rebooting between tests. The prior ABI, close-reuse,
preemption, recording/monitoring and target UDP tests remain required.

## Limits

These are injected syscall failures above a real volatile filesystem, not
natural media failures or a custom failing filesystem driver. They verify the
bounded synchronous adapter's software response, not crash/power-loss atomicity,
persistence, physical storage, real-time deadlines or a threaded storage worker.
Partial/poisoned files are deliberately inspected as invalid output, not
published as accepted records. A terminal error still requires supervisory
handling. Actual hardware remains a separate qualification step.
