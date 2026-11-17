# Buffer placement is a platform decision

**Where should a large frame pool live?** Supply its backing storage when constructing the camera composition. Heap-backed buffers or platform-placed slices avoid requiring large inline values on a worker's stack. Boxing a value after construction may already be too late to avoid that temporary.

The investigation below explains the failure that motivated this interface and its regression coverage. See [memory budgeting](rust-std-footprint.md) for the broader accounting and [native qualification](native-backend.md) for the fixture that uses it.

The first real Windows CLI run found a startup stack overflow (0xC00000FD).
The small mock fixtures passed, but the inline 3 x 65536-byte camera pool
created large unoptimized constructor temporaries on the process stack.

The fix does not enlarge the stack or reduce frame capacity. CameraService
and CameraProduct now accept a backing type B through `with_buffers`. Standard
AsRef<[u8]> / AsMut<[u8]> are sufficient; there is no custom allocator trait,
service registry, unsafe initialization or std dependency in the portable code.

The native runner allocates each payload directly into Box<[u8]> using a
fallible Vec reservation, then moves only small buffer handles into the
composition. Embedded runners may supply separately placed mutable slices
whose lifetime is owned by their board/runtime setup. The default `new` path
retains inline arrays for small fixtures and small products. Do not use it for
large pools unless stack/placement is explicitly qualified. Boxing a completed
inline product is not a guaranteed fix for constructor stack temporaries.

Buffer lengths are validated before device start. Publication still swaps
staging/history, preserves borrowed-frame lifetimes and uses the same cursor,
backpressure and lifecycle logic. Recorder and Monitor policy is unchanged.

A native regression constructs a full 65536-byte-frame product on a 128 KiB
thread stack, records a full frame using the real file worker, and receives
its real UDP summary. Another test qualifies externally supplied mutable
slices and rejects mismatched capacities before device start. The ordinary
Windows CLI remains an independent startup regression with default OS stack
settings. Cortex-M0 and Node/Chromium gates still cover the portable code.

This supplements the native-backend and portable-services guides. Wherever
those guides show `new` with large inline capacities, use platform-placed
storage and `with_buffers` for a production composition instead. This changes
buffer construction/storage representation, not application/service policy.
