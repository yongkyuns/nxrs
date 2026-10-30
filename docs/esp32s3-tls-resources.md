# ESP32-S3 camera/TLS resource qualification

The baseline camera/Wi-Fi/TLS **interoperability fixture**, built with size optimization,
LTO and immediate abort, is **1,027,064 B (1.027 MB / 0.979 MiB)**. This is the
whole NuttX flash image, not TLS alone or a shipping camera product. Measured
source `c507da6d98fbb3016677949f7b737a5ae80ac462` has the same tree as merged
main `409445b24850a9d347245ecd305aa3210d2f90ad`. Image SHA-256:
`d82fe42f9c6892d1e50b2087f67cb571c36844b3dd8a12ce6962f2abcd87f469`.
The build recipe is in [Device TLS](../hal/tls/README.md).

## Flash breakdown

| Retained component | Bytes |
| --- | ---: |
| Vendor radio archives: 802.11, packet processing, PHY, ESP-NOW | 250,154 |
| Radio SDK crypto, inside `libarch.a` | 94,470 |
| Remaining architecture/native SDK, including radio glue | 133,790 |
| Application Mbed TLS/PSA native objects and ABI bridge | 108,018 |
| Rust fixture, services, TLS adapter and application constants | 79,649 |
| Named Rust std/core/alloc sections | 36,036 |
| NuttX scheduler, heap, binary loading and core | 35,296 |
| NuttX networking | 36,162 |
| NuttX VFS/filesystems | 26,990 |
| NuttX generic drivers | 23,817 |
| C/math/compiler runtime | 36,775 |
| NSH and network utilities | 37,683 |
| Startup/board/Xtensa support | 7,511 |
| Merged strings, shared ownership | 48,620 |
| Linker fill/unowned ranges within loadable sections | 7,830 |
| **Loadable sections** | **962,801** |
| Image alignment gaps | 64,203 |
| Image headers/checksum padding | 60 |
| **Flash binary** | **1,027,064** |

These are retained bytes, not additive savings from disabling components.
String merging, Rust inlining and LTO prevent exact per-source ownership;
shared strings are counted once and remain separate. The Rust fixture/services
row includes 36,936 B of named endpoint sections, 10,016 B of stream-service
sections and 9,483 B of TLS-adapter sections. Inlined work can belong to its
caller. No detector model is selected by this profile.

The radio SDK's prefixed crypto and the application's shared Mbed TLS/PSA
are both retained: together, 202,488 B of attributed native code/data. Different
versions/configurations and callers prevent simply removing one. Rust std's
named sections are a smaller part of this image. The fixture retains multiple
protocol cases, synthetic images, file-based test PKI, output and a shell.

The SDK and ABI bridge now compile into separate function/data sections so the
existing linker can discard unused routines. A matched endpoint build with the
same pinned tools, platform, TLS policy and application gives:

| Measurement | Combined sections | Function/data sections | Reduction |
| --- | ---: | ---: | ---: |
| Loadable code/data | 962,801 B | 934,521 B | 28,280 B |
| Flash binary | 1,027,064 B | 1,023,556 B | 3,508 B |
| Static BSS | 83,224 B | 83,216 B | 8 B |

The clean control is `4223a5fffeb9c0d798205c9add660f56a27c1c5a`; the sectioned
build is `f3802e60b2e6c209d3ef28100f4a92ee7714a182`. Alignment absorbs most
of the payload saving. Native SDK/sanitizer/CSR controls and TLS1.2/1.3 peers
pass on the host; this does not qualify physical timing or runtime memory.
The size tool follows SDK input ownership through the partial link, rather
than guessing ownership from C section names.

Next measure the actual camera entry point separately from this multi-case
fixture. Radio configuration and shell trimming need independent comparisons.
Compare loadable sections alongside `.bin` length when evaluating changes.

Reproduce the breakdown without rebuilding or accessing hardware:

```sh
python3 tools/esp32s3-image-size.py target/esp32s3-camera-tls > image-components.json
```

Configured dependencies identify radio-crypto objects; the GNU map supplies
final ranges after relaxation. Discarded/debug sections and dummy address
windows are excluded. JSON records input hashes, section totals and components.

## RAM and physical checks

The baseline reserves **177,016 B of static RAM/address space**: 67,584 B IRAM,
109,372 B DRAM and 60 B RTC storage. This includes initialized data, BSS,
startup stack and linker reservations; aliased DRAM dummy space is counted once.
It excludes dynamic task stacks, camera/DMA buffers, Wi-Fi buffers, TLS heap and
fragmentation. It is **not a runtime peak**. ELF debug data is not flashed.
For example, the profile configures a 614,400 B camera DMA buffer budget;
that is a separate runtime resource, not part of the 1 MB flash figure.

The fixture-only `entropy-probe` executable observes nonblocking `/dev/random`
reads without printing samples or enabling TLS. In the existing build recipe,
use `--app-manifest tests/nuttx-std/wifi/Cargo.toml --app-package nxrs-std-wifi
--bin entropy-probe --command entropy_probe --stack-size 8192` and add both
`--target-c-source tests/nuttx-std/wifi/ffi/entropy-flags.c` and
`--target-c-source tests/nuttx-std/wifi/ffi/credentials.c`.

After preserving the original image, an exclusively owned board can run
`entropy_probe cold` before RF starts, `entropy_probe active` after `ifup wlan0`,
and `entropy_probe stopped` after `ifdown wlan0`. Interface-up alone does not
establish active RF: the profile permits disconnected-station power saving.
`entropy_probe station` reuses the Wi-Fi fixture's raw, no-echo credential input,
association/DHCP, disconnect and joined shutdown; it samples while connected and
again after shutdown. Credentials never enter shell arguments or history.
Confirm actual RF state and
zero NSH exit status each time. It checks 224 reads across seven boundary sizes,
the 32-byte cap, untouched suffixes and unavailable-source refusal. The active
case requires successful output; it reports contention and maximum observed
duration. This cannot preempt a stalled call, establish entropy quality or prove
worst-case latency. Clock resolution also limits timing.

For pre-radio startup observations, add `--kconfig-disable NSH_NETINIT` to the
fixture build and run `entropy_probe cold` before any station command. The
builder records and verifies the override while preserving profile requirements
and forbidden-option checks. A reset/startup observation does not qualify a
power-loss cycle or the bootloader's earlier entropy source.

On a physical ESP32-S3, three station connection/shutdown cycles produced:

| State | Successful reads | Bytes | Unavailable reads |
| --- | ---: | ---: | ---: |
| Associated, DHCP complete | 672 | 12,960 | 0 |
| Disconnected, provider joined | 0 | 0 | 672 |

Each cycle passed the 32-byte cap, untouched-suffix and zero-NSH-status checks.
The maximum observed read was 1 ms with a 1 ms scheduler tick; this is neither
an entropy-quality assessment nor a worst-case latency bound. The clean measured
source is `2db83dd03fbd7730fa23e60552b5722e02e7907f`; image SHA-256 is
`230538a4b5036d6e65118f91df7edc9c4dfaa337230f95c83a6604492d4c4d53`.
Interface-up without association had refused every read, so it is insufficient
as the active test prerequisite. This observation does not identify which driver
guard caused refusal or independently qualify disconnected power saving.
The original 16 MiB flash was restored and separately verified. Its camera
supervisor resumed, and three advancing 320×240 source JPEGs decoded on the Mac;
this recovery check does not qualify gateway or phone playback.

Concurrent camera load, TLS handshake/reseed latency and heap/stack peaks remain
unmeasured. Pre-radio startup/reset coverage and source-health gates are tracked
in [issue #35](https://github.com/yongkyuns/nxrs/issues/35).
Existing NSH `free`, `ps` and `/proc/meminfo`
support resource observations; snapshots alone cannot establish peaks.
ESP32-S3 TLS admission remains disabled.
