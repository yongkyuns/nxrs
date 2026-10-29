# ESP32-S3 camera/TLS resource qualification

The camera/Wi-Fi/TLS **interoperability fixture**, built with size optimization,
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

First measure the actual camera entry point separately from this multi-case
fixture. Next, measure whether function/data sections allow the native TLS linker
to discard unused routines; its C objects currently contain combined sections.
Keep the protocol/failure tests when trying that change. Radio configuration
and shell trimming require separate comparisons. Alignment can hide or amplify
growth, so compare loadable sections alongside `.bin` length.

Reproduce the breakdown without rebuilding or accessing hardware:

```sh
python3 tools/esp32s3-image-size.py target/esp32s3-camera-tls > image-components.json
```

Configured dependencies identify radio-crypto objects; the GNU map supplies
final ranges after relaxation. Discarded/debug sections and dummy address
windows are excluded. JSON records input hashes, section totals and components.

## RAM and physical checks

The image reserves **177,016 B of static RAM/address space**: 67,584 B IRAM,
109,372 B DRAM and 60 B RTC storage. This includes initialized data, BSS,
startup stack and linker reservations; aliased DRAM dummy space is counted once.
It excludes dynamic task stacks, camera/DMA buffers, Wi-Fi buffers, TLS heap and
fragmentation. It is **not a runtime peak**. ELF debug data is not flashed.

The fixture-only `entropy-probe` executable observes nonblocking `/dev/random`
reads without printing samples or enabling TLS. In the existing build recipe,
replace `--bin tls-endpoint --command tls_endpoint --stack-size 65536` with
`--bin entropy-probe --command entropy_probe --stack-size 8192`, and add
`--target-c-source tests/nuttx-std/tls-identity/entropy-flags.c`.

After preserving the original image, an exclusively owned board can run
`entropy_probe cold` before RF starts, `entropy_probe active` after `ifup wlan0`,
and `entropy_probe stopped` after `ifdown wlan0`. Confirm actual RF state and
zero NSH exit status each time. It checks 224 reads across seven boundary sizes,
the 32-byte cap, untouched suffixes and unavailable-source refusal. The active
case requires successful output; it reports contention and maximum observed
duration. This cannot preempt a stalled call, establish entropy quality or prove
worst-case latency. Clock resolution also limits timing.

Physical RF transitions, concurrent camera load, TLS handshake/reseed latency,
heap/stack peaks and repeated teardown/recovery remain unmeasured for this
image. Existing NSH `free`, `ps` and `/proc/meminfo` support resource observations;
snapshots alone cannot establish peaks. ESP32-S3 TLS admission remains disabled.
