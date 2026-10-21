# NuttX descriptor ownership and failed close

## The reviewed bug

The initial Rust OwnedFd retained its descriptor number when close returned
an error. That assumed the number still referred to the original resource.
At the pinned NuttX revision this is false: fdlist_close uninstalls the fd
before file_put reports a driver close error. Retrying the number, including
from Drop, can close a different application's newly opened resource.

Source inspected: NuttX `89434949d51104c8363111d23dcdaf22e7d499ab`,
`fs/inode/fs_files.c` (fdlist_close/file_put) and `fs/vfs/fs_close.c`.
This conclusion is tied to that implementation, not a universal claim about
all POSIX systems or every future NuttX version.

## Repaired semantics

The legacy `OwnedFd` and read-device `DeviceCamera` now live under
`tests/nuttx/support`, alongside their core-only qualification. Production video
capture owns its `File` in Rust and uses camera-local `close.c` only to return
the status of an explicitly consumed close. The legacy fixture's measured
results are not qualification of a newly changed platform implementation.

OwnedFd consumes its number before calling the C bridge, whether the call
succeeds or fails. It stores any close error. Repeated close requests return
that error without another syscall; access to a consumed descriptor also
fails before entering C. Drop never retries a consumed number.

A DeviceCamera close failure therefore remains terminal and visible. The
owner cannot restart the same failed instance, and a service waiting for
successful shutdown must not reinterpret repeated failure as completion.
This does not prove that a failing physical driver cleaned up its resources;
a supervisor must escalate terminal cleanup failure rather than loop forever.
The portable Camera stop contract permits retryable failures, but a retry
cannot resurrect a consumed OS descriptor. Kernel/driver lifetime and Rust
handle lifetime must be treated separately.

## Shared sim and QEMU regression

A small test-only probe is registered as a genuine NuttX read device. Its
normal close succeeds, releasing its inode and descriptor. The test build
interposes only the bridge's close symbol, calls the real bridge/target close,
opens /dev/null to reuse the exact released number, then returns a synthetic
I/O error. The number must actually be reused; the test cannot pass vacuously.
This models the real consumed-descriptor/error case without creating a
lower-half close failure that leaks an inode in the pinned kernel.

The Rust test then checks that reads fail without modifying the destination,
restart is rejected, four stop retries preserve the error, and Drop leaves
the sentinel open. Only one bridge close invocation is permitted. Finally,
the C verifier deliberately closes the sentinel and requires EBADF as a
negative control. The probe is unregistered, so the second same-kernel
invocation exercises fresh registration and cleanup.

The Python transcript oracle requires exactly one RC_TARGET_CLOSE result per
invocation in both sim and QEMU. Self-tests reject missing/duplicate results,
unobserved descriptor reuse, multiple close attempts, lost sentinel ownership,
and a broken negative control. Existing sensor open/close counters and frame
expectations are unchanged because the probe is a separate device.

All fault controls live in tests/nuttx/c and tests/nuttx's qualification
module, not the portable HAL/services. The production bridge source still
calls normal NuttX close; test-build symbol interposition is explicit in the
fixture Makefile. This is descriptor-lifetime qualification, not physical
driver teardown, persistent-storage failure or power-loss qualification.
