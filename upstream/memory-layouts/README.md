# Bundled BSP memory references

The viewer uses `tools/memory-explorer/layouts/catalog.json` offline. It covers every native MCU
BSP selected by the repository's product and qualification profiles. Simulator,
desktop and web targets retain their process/linear address spaces rather than
receiving an invented MCU bank map.

Each descriptor cites the exact source revision from which its geometry was
reviewed. This directory's `sources.json` inventories compact, **verbatim reference files**, their
upstream URLs, complete commit IDs, byte lengths and SHA-256 digests. Source files
are reference material; the viewer never compiles or executes them. CoreS3's BSP
is an NXRS patch over the pinned NuttX common ESP32-S3 scripts, so that patch is
retained with its original repository/commit identity as well.

## Verify or restore

From the repository root:

```sh
python3 tools/memory-explorer/refresh_bsp.py --check
python3 tools/memory-explorer/refresh_bsp.py --download
```

The first command checks the source inventory and the JSON/Rust catalogue
agreement without using the network. The second fetches only the exact
manifest-pinned URLs and verifies every response before replacing any reference
file. It does not select a newer version or execute downloaded content.

To qualify a new upstream revision, review its BSP linker scripts, memory-map
definitions, aliases, runtime configuration and relevant local patches. Update
the manifest pins and file hashes only after examining those source changes;
then update descriptors, the catalogue and its Rust table together. Run the
catalogue tests and viewer model tests. A version bump alone does not qualify
unchanged geometry for a different target.

## Scope of the maps

| Family | Known geometry | Configured or unobserved geometry |
| --- | --- | --- |
| MPS2-AN521 QEMU | QEMU 10.0.13 SSRAM, SSE SRAM, system RAM and linear aliases | Security permissions and physical-board memory are not attested |
| ESP32-S3 CoreS3 / DevKit / WROOM | 512 KiB internal SRAM counted once across IRAM/DRAM aliases; two 8 KiB RTC banks | Flash/PSRAM MMU windows have no assumed physical mapping; exact matched build configuration can add capacity-only resources |
| Pico 2 / Pico 2 W | 512 KiB main SRAM group plus two 4 KiB scratch banks, in normal striped CPU coordinates | Individual striped-bank addressing and QMI flash translation are unobserved; the 4 MiB XIP window is a BSP linker budget |

Capacities include reservations, not just allocator storage. Unknown bytes never
become free heap. A configured external-memory capacity is a build declaration,
not a physical-device measurement or an assertion that an MMU window maps all
of that memory.

The Pico 2 address-header comments contain stale end addresses and flash size.
Its descriptor instead uses the numeric start definitions, all three board
linker scripts, and the effective SRAM size in both native BSP defconfigs.
The tests independently compare those sources to the descriptor.

## Third-party notices

Original copyright and license headers remain in every retained source file.
NuttX and the retained Espressif files permit Apache-2.0 use; its complete text
is in `sources/esp-hal-3rdparty/LICENSE`, with NuttX's notice retained in
`sources/nuttx/NOTICE`. The QEMU reference files remain under their original GPL
terms; `sources/qemu/COPYING` contains the GPL-2.0 text. These reference licenses
are not replaced by the NXRS repository's license. The manifest lists these
license files alongside the source records.
