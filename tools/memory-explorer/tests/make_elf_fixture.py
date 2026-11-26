#!/usr/bin/env python3
"""Build a real ELF/link map with known placement; it is not device evidence."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from test_gdb_export import generate_synthetic_capture


APPLICATION = """\
const char greeting[] = "NXRS memory fixture";
volatile unsigned initialized = 42;
unsigned workspace[32];
void application(void) { workspace[0] = initialized + greeting[0]; }
"""

STARTUP = """\
extern void application(void);
void reset(void) { application(); for (;;) {} }
"""

LINKER = """\
ENTRY(reset)
MEMORY {
  FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 64K
  RAM (rw) : ORIGIN = 0x20000000, LENGTH = 16K
}
SECTIONS {
  .text : { *(.text*) } > FLASH
  .rodata : { *(.rodata*) } > FLASH
  .data : { *(.data*) } > RAM AT> FLASH
  .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM
  .heap 0x20001000 (NOLOAD) : {
    __heap_start = .;
    . += 0x1000;
    __heap_end = .;
  } > RAM
  /DISCARD/ : { *(.eh_frame*) *(.comment*) *(.note*) }
}
"""


def generate(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for name, source in (("application", APPLICATION), ("startup", STARTUP)):
        (output / f"{name}.c").write_text(source, encoding="utf-8")
        subprocess.run(
            ["cc", "-c", "-g", "-O0", "-ffreestanding", "-fno-pie",
             "-fno-asynchronous-unwind-tables", "-ffunction-sections", "-fdata-sections",
             f"{name}.c", "-o", f"{name}.o"],
            cwd=output, check=True,
        )
    (output / "fixture.ld").write_text(LINKER, encoding="utf-8")
    subprocess.run(
        ["ld", "-T", "fixture.ld", "-Map=fixture.map", "-o", "fixture.elf",
         "startup.o", "application.o"],
        cwd=output, check=True,
    )
    # Expectations come from this linker's fixed regions and the C object sizes,
    # independently of the explorer's importer.
    expected = {
        "description": "Host-linked ELF with synthetic MCU addresses; not firmware execution",
        "elf_sha256": hashlib.sha256((output / "fixture.elf").read_bytes()).hexdigest(),
        "text_start": 0x08000000,
        "data_start": 0x20000000,
        "data_size": 4,
        "bss_size": 128,
        "heap_start": 0x20001000,
        "heap_size": 0x1000,
        "greeting_size": 20,
    }
    (output / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    layout = {
        "schema_version": 1,
        "physical_resources": [
            {"id": "fixture-flash", "name": "Synthetic fixture flash", "size": 65536},
            {"id": "fixture-sram", "name": "Synthetic fixture SRAM", "size": 16384},
        ],
        "regions": [
            {
                "id": "fixture-flash-window", "name": "Synthetic FLASH window",
                "start": 0x08000000, "size": 65536, "kind": "flash", "domain": "cpu_virtual",
                "physical": {"id": "fixture-flash", "offset": 0},
            },
            {
                "id": "fixture-ram-window", "name": "Synthetic RAM window",
                "start": 0x20000000, "size": 16384, "kind": "ram", "domain": "cpu_virtual",
                "physical": {"id": "fixture-sram", "offset": 0},
            },
        ],
    }
    (output / "layout.json").write_text(json.dumps(layout, indent=2) + "\n", encoding="utf-8")
    generate_synthetic_capture(output / "fixture.elf", output / "exporter-snapshot.json")
    return expected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.output.resolve()), indent=2))
