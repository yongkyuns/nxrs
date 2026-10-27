/* Emulator witness only; below, and disjoint from, actual bootloader RAM. */
ENTRY(fixture_entry)
MEMORY {
  iram (rx) : ORIGIN = 0x40378000, LENGTH = 0x10000
  dram (rw) : ORIGIN = 0x3fc90000, LENGTH = 0x10000
}
SECTIONS {
  .iram : { *(.literal .literal.*) *(.text .text.*) . = ALIGN(4); } > iram
  .dram : { *(.rodata .rodata.*) *(.data .data.*) . = ALIGN(4); } > dram
  .bss : { *(.bss .bss.*) *(COMMON) } > dram
  /DISCARD/ : { *(.eh_frame) *(.comment) *(.xtensa.info) }
}
