# NXRS Memory Explorer

Open a final firmware ELF, its GNU linker map, and captured NuttX heap/stack
snapshots in a native egui viewer. The address canvas preserves placement and
gaps while you zoom, pan, search, and inspect individual extents. Source colors
connect linked objects and recorded allocation origins to those extents.

This is the offline foundation of [issue #442](https://github.com/yongkyuns/nxrs/issues/442).
It shows static placement and discrete lifecycle observations. Continuous
allocation-event replay, live transport, and physical-board qualification are
follow-up work in that issue.

## Open the viewer

Run from the NXRS repository root with its pinned Rust 1.90 toolchain:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- --demo
```

The demonstration contains invented addresses, sources, snapshots, and backing
mappings. Its banner identifies it as synthetic. Use it to try the controls
before collecting firmware evidence.

The package is a non-default workspace member. Its library has no GUI dependency
unless `gui` is selected; egui/eframe stays in this host tool. The native viewer
uses eframe 0.33.3 with the Glow renderer and keeps the repository's existing
Rust toolchain. Linux needs a graphical session, an OpenGL implementation, and
the native X11/Wayland development prerequisites; the focused
[workflow](../../.github/workflows/memory-explorer.yml) records the Linux packages
used for CI. Native build/test jobs also cover macOS and Windows.

For an existing build, supply the exact final ELF and its map. For example, after
the normal [NuttX build](../../docs/build-system.md) for `std-demo`:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/build/std-demo/mps2-an521-mock/nuttx/nuttx \
  --map target/build/std-demo/mps2-an521-mock/nuttx/nuttx.map
```

The ELF alone is enough to see linked sections and symbol labels. The map adds
retained object/archive contributions. It must come from the same link: the
importer checks matching allocated output-section addresses and sizes, but a
GNU map has no cryptographic build identity of its own.

An empty invocation opens the input form. **Open files** also opens it; paste
paths or drag an ELF, `.map`, and JSON files into the window, then select **Load
memory evidence**. `--help` prints all command options without opening a window.

## Navigate an address space

| Task | Control |
| --- | --- |
| Choose a CPU/image window or physical resource | Select its sidebar card |
| Inspect exact range, source, role, and containing object | Click a block |
| Fit a block | Double-click it, or select **Zoom to block** |
| Fit the selected region | **Fit region**, `F`, or `Home` |
| Zoom around an address | Scroll over the canvas; the pointer anchors the zoom |
| Pan | Drag the canvas, scroll horizontally, or press the left/right arrow key |
| Zoom with the keyboard | `+` / `-` |
| Jump to an exact address | Enter hexadecimal in the address field and select **Go** |
| Find a source, section, object, or symbol | Use the sidebar search; select a result to focus it |
| Inspect an allocator arena or task stack | Select it under **Captured arenas and stacks** |
| Choose an observation | Click a timeline point, **Previous** / **Next**, or `Page Up` / `Page Down` |
| Inspect a gap between observations | Click between timeline points |
| Change coordinates | **Runtime / image** or **Physical backing** |
| Hide optional ELF symbol labels | Clear **ELF symbols** |

Search dims nonmatching blocks without moving them. Byte geometry therefore
stays meaningful during filtering. Small or zero-size symbol labels use ticks;
they are annotations and do not add occupied bytes. The inspector exposes full
labels when a block is too narrow to contain its text.

The canvas has separate layers for linked sections, source objects/symbols,
captured heap or pool state, and task stacks. A task stack contained in an
allocated chunk overlays that allocation; it does not consume the same bytes a
second time. Source colors have a legend, while free, reserved, and unknown
states have distinct styling and explicit labels.

## Read the coordinates correctly

| View | What an address means | Evidence required |
| --- | --- | --- |
| CPU window | The ELF's linked virtual memory address, or the address recorded by a snapshot | ELF sections and/or capture |
| ELF file offsets | A byte offset in the supplied ELF container | Section file ranges |
| Physical backing | An offset within a named storage resource with a declared capacity | Explicit layout mapping |

CPU addresses do not establish unique physical storage. Two CPU windows can
alias the same SRAM. A supplied layout maps each window to a resource and byte
offset, and the physical view uses that resource's full capacity. Accounting
unions aliased ranges rather than adding their bytes twice.

Without a layout, inferred ELF windows cover the allocated sections visible in
the file. They do not establish total board RAM or flash capacity. The viewer
labels their backing as unknown. Empty space is never inferred to be free heap:
only captured `free` chunks establish that state.

The `.data` section has CPU storage and file-backed initializer bytes. `.bss`
has CPU storage without file bytes. Other NOBITS sections, including `.heap` and
`.noinit`, are reservations whose initialization policy cannot be inferred from
NOBITS alone. Thread-local storage templates do not establish the addresses or
number of live per-thread instances, so the importer warns and avoids inventing
those CPU allocations.

ELF offsets are not programmed flash offsets. This version does not project
`PT_LOAD` physical/load addresses, `.data` load copies, bootloader headers, or
vendor image transformations into a programmed-image view. It also requires a
final `ET_EXEC` ELF with allocated section headers; relocatable objects, archives,
PIE/`ET_DYN` files needing a load bias, and section-header-free images are rejected.

## Capture a NuttX lifecycle point

The [GDB exporter](gdb_export.py) reads the default NuttX allocator and task
metadata. Its supported layout is the repository's pinned NuttX revision
`2b5c0de5e933281c209f954a63b99cf10c132346`. Use a Python-enabled target GDB,
the pinned NuttX `pynuttx` dependencies, and the exact final ELF with allocator
and task debug types. Follow the target's debugger procedure to connect and
halt every relevant CPU at a known quiescent lifecycle point.

In that GDB session, with the repository root as the working directory:

```gdb
source external/nuttx/tools/pynuttx/gdbinit.py
source tools/memory-explorer/gdb_export.py
nxrs-memory-export /tmp/ready.json --label ready --time-ns 0
```

Resume the application through the workload, stop again at the intended
observation boundary, and export another file:

```gdb
nxrs-memory-export /tmp/after-session.json --label after-session --time-ns 1
```

Here `0` and `1` are manually supplied ordering coordinates. The exporter does
not sample a target clock or claim a one-nanosecond workload. Supply measured
nanosecond coordinates only when an independent clock establishes them. Every
loaded point needs a distinct coordinate. Each output file is created
exclusively, so a new capture cannot silently overwrite an earlier one.

The exporter discovers registered heaps when available and otherwise uses
`g_mmheap`. Repeated `--heap` options select named globals explicitly, for
example `--heap g_mmheap --heap g_kmmheap`, when those symbols exist in the
selected build. The option accepts symbol names, not arbitrary GDB expressions.

Open the captures with their ELF:

```sh
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/build/std-demo/mps2-an521-mock/nuttx/nuttx \
  --map target/build/std-demo/mps2-an521-mock/nuttx/nuttx.map \
  --snapshot /tmp/ready.json \
  --snapshot /tmp/after-session.json
```

Each snapshot includes the SHA-256 of GDB's loaded host ELF. The importer refuses
to combine it with a different ELF. This guards against mixing host artifacts;
it does not attest that the device is running those bytes.

The exporter checks that every thread GDB exposes is stopped and that raw chunk
extents form a valid arena. It does not call target functions, drain deferred
frees, read allocation payloads, or modify target state. A debugger can hide a
CPU, and valid topology does not prove the allocator was stopped between
transactions. Choose an application quiescence point appropriate to the target.

### What the snapshot establishes

| Record | Meaning and limit |
| --- | --- |
| Arena and chunks | Raw allocator extents including allocator overhead and guard nodes |
| Free chunk | Explicit observed allocator free extent; alignment and metadata can reduce a usable request |
| Deferred free | Remains allocated until the allocator actually reclaims it |
| Allocation source | First recorded backtrace PC resolved through host debug information, when available; raw PCs are retained |
| PID/sequence metadata | Allocator-recorded fields when present; not a durable allocation lifetime ID or current semantic owner |
| Task stack | Usable reserved bounds when they can be represented within the observed containing chunk |
| Stack use/high-water | Unknown in this adapter; no coloration scan is performed |
| Pool backing | Parent allocation visible; individual slot occupancy unknown |

Some NuttX shared-boundary layouts put usable stack bounds outside the strict
raw chunk interval. The exporter preserves their raw task metadata and emits a
warning instead of drawing a false contained stack. Missing backtrace/debug
information leaves source attribution unknown. Static map colors identify
objects or archive members; they do not claim unique original Rust source lines
under LTO, code folding, or merged constants.

Per-arena statistics expose allocated chunk bytes, reserved bytes, free chunk
bytes, largest free chunk, and unobserved bytes. They are scoped to one arena:
free space in a nested pool is not interchangeable with free space in its
parent heap. Requested allocation sizes remain unknown, so this adapter does
not calculate internal fragmentation from guessed payload lengths.

Only exact captured points have runtime state. Between points the runtime layers
are hidden and marked unknown. Pointer reuse and allocate/free operations between
those points cannot be reconstructed from snapshots.

## File format

Version 1 uses UTF-8 JSON for offline observations and an optional separate
layout file. The authoritative Rust types and validation live in
[model.rs](src/model.rs); the ELF/map adapter lives in [import.rs](src/import.rs).
The model is independent of egui so another capture transport can populate it
without changing geometry or accounting rules.

All addresses, sizes, and coordinates are unsigned 64-bit JSON integer numbers.
Hexadecimal is a display/input convenience in the GUI, not a JSON syntax. Ranges
are nonempty half-open intervals `[start, start + size)`; overflow is rejected.
Producers must preserve integer precision, including values above JavaScript's
exact-number range.

| Snapshot field | Contract |
| --- | --- |
| `schema_version` | Exactly `1`; unsupported versions fail |
| `build` | `elf_sha256` as 64 hex digits, plus optional `label` |
| `regions` | Named address windows: `id`, `name`, `start`, `size`, `kind`, `domain`, optional `physical` |
| `physical_resources` | Optional named resource capacities: `id`, `name`, `size` |
| `snapshots` | Ordered observations with `time_ns`, `label`, `spans`, optional `complete_regions` |
| `warnings` | Optional evidence limitations displayed by the viewer |
| `capture` | Optional exporter-specific metadata retained in the input file; not permission to weaken validation |

A span contains `id`, `name`, `region`, `start`, `size`, and `kind`, plus optional
`source`, `parent`, and `detail`. Kinds are `code`, `read_only`, `data`, `bss`,
`heap_arena`, `allocation`, `free`, `stack`, `stack_used`, `pool`, `reserved`, and
`unknown`. Parents represent containment; sibling overlaps, missing parents,
cycles, out-of-region spans, and conflicting free/live evidence fail validation.
Symbols stay separate from ownership spans because legal symbol aliases overlap.

`complete_regions` asserts complete chunk coverage of the captured arenas in
those regions. It does not assert that every byte in a larger board RAM window
was observed. Missing arena coverage stays unknown unless a complete-coverage
claim requires the importer to reject it.

### Declare board storage

A layout contains `schema_version`, `regions`, and `physical_resources`. The
following complete example describes an invented 8 KiB SRAM window at
`0x20000000`; replace it with the selected board/profile's verified map:

```json
{
  "schema_version": 1,
  "physical_resources": [
    { "id": "sram", "name": "Example SRAM", "size": 8192 }
  ],
  "regions": [
    {
      "id": "ram",
      "name": "Example CPU SRAM window",
      "start": 536870912,
      "size": 8192,
      "kind": "ram",
      "domain": "cpu_virtual",
      "physical": { "id": "sram", "offset": 0 }
    }
  ]
}
```

Pass it with `--layout layout.json`. A mapping translates `region.start` to
`physical.offset` in the named resource and preserves byte offsets throughout
the window. Another nonoverlapping CPU window can map to the same backing range
to declare an alias. Bounds and conflicting observations are checked. A larger
layout window may contain a snapshot's smaller heap window; the importer remaps
the capture only when containment and any explicit backing agree.

Version 1 only expresses linear mappings. It does not infer ESP32 IRAM/DRAM
aliases or RP2350 striped bank mappings. Leave unsupported backing unmapped
until a validated board adapter can represent it. This keeps CPU geometry useful
without presenting invented physical precision.

## Verify changes

The model/import tests run without a graphics dependency. On Linux the independent
fixture also requires Python 3, `cc`, and GNU `ld`:

```sh
cargo test --locked -p nxrs-memory-explorer
python3 -m unittest discover -s tools/memory-explorer/tests -p test_gdb_export.py -v
cargo test --locked -p nxrs-memory-explorer --features gui --bin nxrs-memory-explorer
cargo clippy --locked -p nxrs-memory-explorer --all-targets --features gui -- -D warnings
```

The linked fixture is built by the system compiler/linker from fixed-address C
inputs. The exporter fixture runs the real Python serializer against explicitly
synthetic debugger objects. Their integration checks exact placement, map
attribution, NOBITS treatment, nested stack accounting, hash mismatch rejection,
and unknown intervals independently of the GUI's demo.

To render that fixture locally:

```sh
python3 tools/memory-explorer/tests/make_elf_fixture.py target/memory-explorer-fixture
cargo run --locked -p nxrs-memory-explorer --features gui -- \
  --elf target/memory-explorer-fixture/fixture.elf \
  --map target/memory-explorer-fixture/fixture.map \
  --layout target/memory-explorer-fixture/layout.json \
  --snapshot target/memory-explorer-fixture/exporter-snapshot.json
```

Add `--smoke-test --screenshot /tmp/nxrs-memory-smoke.ppm` to exercise geometry,
navigation, and available evidence paths, save the actual rendered framebuffer,
and exit. CI runs the demo and linked fixture through a software-rendered Xvfb
session and uploads their PPM images. Inspect those images as well as the test
results. Host fixtures and rendered UI checks do not qualify a live NuttX GDB
capture or a physical board's memory map.

For existing lifecycle retention and stack high-water checks, continue using
[NuttX heap and stack qualification](../../docs/nuttx-memory-qualification.md)
and [Rust allocation qualification](../../docs/memory-qualification.md).
