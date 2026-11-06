# Rust std patches

The normal NuttX build applies
[native pollfd initialization](patches/0001-native-pollfd-initialization.patch)
alongside the [libc layout fix](../libc/patches/0001-nuttx-pollfd-abi.patch).
Preparation patches only a private, source-pinned SDK; the target ABI gate
checks the actual compiled binding before firmware linking. See
[patchset provenance](../../docs/upstream-patchsets.md).

These are compatibility fixes for the selected NuttX ABI. The channel and
parking implementations below make separate choices about RTOS scheduling and
handoff costs. Upstream [bounded channels](https://doc.rust-lang.org/std/sync/mpsc/)
already provide a preallocated buffer of fixed size.

## Native NuttX channel and parking

NuttX bounded channels use one Rust-owned transaction for the ring and FIFO
waiters. Completion, timeout, disconnect and ownership stay in Rust; allocation,
message destruction and native wakeups occur after unlocking. Native parking
uses a coalesced semaphore permit with monotonic deadlines and EINTR handling.
This implements std's existing
[single-permit notification contract](https://doc.rust-lang.org/std/thread/fn.park.html).
Zero-capacity and unbounded channels retain the upstream algorithms. Their
operation boundaries use `#[cfg_attr(target_os = "nuttx", inline(never))]` so
NuttX flavor dispatch does not copy those algorithms into bounded-channel
callers. This also lets product linker policy keep alternate flavors in flash.

Preparation selects the new queue and parker only for `target_os = "nuttx"`,
once at the OS boundary. Both pinned Rust SDKs consume the same implementation
bodies; only their module selectors differ.
The SDK ledger records source preimages, patches and the tiny native lock binding.
The NuttX build verifies and stages that binding automatically, including when
reusing a prepared SDK. Apps need no feature or native source argument. Installed
SDKs remain unchanged, and normal host builds use their prebuilt std. Completion
publication does not borrow the whole waiter across its final release store;
the owner may retire that stack immediately.
A retained `Thread` owns notification after publication.

The normal target ABI gate also checks semaphore storage/alignment and
`SEM_PRIO_NONE`. See the [ownership controls](../../tests/nuttx-std/channels/README.md)
and [event-driven performance analysis](../../docs/std-channel-performance.md) for
validation and remaining limits.

The [physical campaign summary](https://github.com/yongkyuns/nxrs/blob/3410313500a29d31c5c45d2183cd3bc599969ae7/docs/std-channel-performance.md)
is retained at revision `3410313500a29d31c5c45d2183cd3bc599969ae7`.
The subsequent target guard on outlining preserves the NuttX `inline(never)`
setting and all runtime bodies. No hardware rerun accompanies that scope change.
The host runner requires a prepared SDK and deliberately rebuilds std with the
queue selector overridden. Its controls check queue semantics with host locking
and parking. They do not qualify NuttX IRQ behavior or performance on other
targets and channel flavors. The recorded device results cover the
combined queue, parker, context-switch and placement configuration.

## Optional NuttX std math RFC

This is an **inactive evaluation patch**, not a replacement for the qualified
std preparation or an installed SDK. The
[proposal manifest](proposals/series.json) pins the Rust revision, three source
preimages and patch bytes. The change belongs in std, not an application
wrapper or Cargo feature.

For `target_os = "nuttx"`, `f32`/`f64` `asinh`, `acosh` and `atanh` call the
corresponding C libm routines. Other targets retain their original formulas.
NuttX's selected libm must provide all six symbols. This makes C and Rust use
the same math implementation; it does not promise correctly rounded results
for every input, stable NaN payloads, or identical `errno` behavior.

The [matched device report](../../tests/arithmetic-parity/results/esp32s3-std-math-2026-10-06.json)
contains three full ESP32-S3 runs, with unchanged compiler, kernel, inputs and
C flags. All six changed math cases agree bit-for-bit and stay within 1 ULP
of their oracle in that corpus. There are no arithmetic errors. The remaining
six raw differences across all three runs are signed-zero choices in
`f32::min/max`, which the suite's contract permits.

The new wrappers are 49 B (`f32`) and 51 B (`f64`), versus 99–184 B for the
old formulas. These are symbol sizes, not full dependency or firmware deltas.
`f64::asinh` becomes faster, but some old formulas were faster than the chosen
libm implementation. Consistent implementation and fewer wrapper instructions
do not mean every math function is faster.

Use the [same local/CI preparation](../rust-llvm/BUILDING.md#optional-std-math-rfc)
to archive an independent qualified std snapshot, prepare a fresh patched
copy and pass its provenance to the compiler builder. The applicator rejects
wrong pins, unsafe archives and reapplication; the builder reverses the
public patch to verify its original preimages, then checks the complete
packaged source inventory. The std ledger is recorded separately from LLVM
in compiler, arithmetic build and public measurement provenance.

No normal build, SDK or CI job is activated by adding this RFC.
