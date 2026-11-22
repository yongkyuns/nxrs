#!/usr/bin/env python3
"""Add UART2, qualified entropy and tracker touch input to ESP-EMU 1.2.0 Wasm."""

import argparse
import hashlib
from pathlib import Path
from shutil import copyfile

from patch_uart_clock import patch, prepend_function_body


GLUE_SHA256 = "693a81d2bd32d68bef108ff2b5e833950abfc5da4cc9feefecb94ac69130506d"
UART2_BASE = 0x6002E000
UART2_SOURCE = 29
QEMU_ENTROPY_ID = 0x600263F8
QEMU_SIGNATURE = 0x51454D55
WDEV_RND = 0x6003507C
S3_DRAM_START = 0x3FC88000
S3_DRAM_LAST_WORD = 0x3FCDFFFC


def insert_before(source, marker, addition):
    if source.count(marker) != 1:
        raise ValueError(f"unexpected v1.2.0 insertion point {marker!r}")
    return source.replace(marker, addition + marker, 1)


def add_devices(module):
    module = insert_before(module, ' (import "./esp_emu_bg.js" "__wbindgen_object_drop_ref"', """
 (import "nxrs_browser" "uart2_read" (func $nxrs_rx (param i32) (result i32)))
 (import "nxrs_browser" "uart2_write" (func $nxrs_tx (param i32 i32)))
 (import "nxrs_browser" "uart2_irq" (func $nxrs_irq (result i32)))
 (import "nxrs_browser" "entropy_available" (func $nxrs_entropy_available (result i32)))
 (import "nxrs_browser" "random_word" (func $nxrs_random_word (result i32)))
""")
    # $784 sets a peripheral source level in the S3 interrupt matrix. The
    # existing paths for UART0 and UART1 call it with sources 27 and 28.
    module = module.rstrip()[:-1] + f"""
 (global $nxrs_uart2_bus (mut i32) (i32.const 0))
 (func $nxrs_refresh_uart2 (export "nxrs_refresh_uart2")
  (if (global.get $nxrs_uart2_bus)
   (then (call $784 (global.get $nxrs_uart2_bus) (i32.const {UART2_SOURCE}) (call $nxrs_irq)))))
 (func (export "nxrs_reset_uart2") (global.set $nxrs_uart2_bus (i32.const 0)))
 (func $nxrs_read_uart2 (param $bus i32) (param $offset i32) (result i32)
  (local $word i32)
  (global.set $nxrs_uart2_bus (local.get $bus))
  (local.set $word (call $nxrs_rx (local.get $offset)))
  (call $nxrs_refresh_uart2)
  (local.get $word))
 (func $nxrs_write_uart2 (param $bus i32) (param $offset i32) (param $word i32)
  (global.set $nxrs_uart2_bus (local.get $bus))
  (call $nxrs_tx (local.get $offset) (local.get $word))
  (call $nxrs_refresh_uart2))
 (func (export "nxrs_touch_word") (param $emu i32) (param $address i32) (param $word i32) (result i32)
  (local $bus i32) (local $region i32) (local $slot i32) (local $offset i32) (local $buffer i32)
  (if (i32.or (i32.eqz (local.get $emu)) (i32.and (local.get $address) (i32.const 3)))
   (then (return (i32.const 0))))
  (if (i32.or
       (i32.lt_u (local.get $address) (i32.const {S3_DRAM_START}))
       (i32.gt_u (local.get $address) (i32.const {S3_DRAM_LAST_WORD})))
   (then (return (i32.const 0))))
  ;; Resolve the same direct RAM mapping that read_memory uses ($1137).
  (local.set $bus (i32.add (local.get $emu) (i32.const 16)))
  (local.set $region (call $1027 (local.get $bus) (local.get $address)))
  (if (i32.eqz (local.get $region)) (then (return (i32.const 0))))
  (if (i32.or (i32.load (local.get $region))
       (i32.gt_u (i32.add (local.get $address) (i32.const 4)) (i32.load offset=16 (local.get $region))))
   (then (return (i32.const 0))))
  (local.set $slot (i32.load offset=4 (local.get $region)))
  (if (i32.ge_u (local.get $slot) (i32.load offset=58392 (local.get $bus)))
   (then (return (i32.const 0))))
  (local.set $buffer (i32.add (i32.load offset=58388 (local.get $bus))
                            (i32.shl (local.get $slot) (i32.const 4))))
  (local.set $offset (i32.add (i32.load offset=8 (local.get $region))
                    (i32.sub (local.get $address) (i32.load offset=12 (local.get $region)))))
  (if (i32.gt_u (i32.add (local.get $offset) (i32.const 4)) (i32.load offset=8 (local.get $buffer)))
   (then (return (i32.const 0))))
  (i32.store (i32.add (i32.load offset=4 (local.get $buffer)) (local.get $offset)) (local.get $word))
  (i32.const 1))
)
"""
    offset = f"(i32.sub (local.get $1) (i32.const {UART2_BASE}))"
    uart2 = f"(i32.lt_u {offset} (i32.const 128))"
    module = prepend_function_body(module, "$105 (param $0 i32) (param $1 i32) (result i32)", f"""
  (if {uart2} (then (return (call $nxrs_read_uart2 (local.get $0) {offset}))))
  (if (i32.eq (local.get $1) (i32.const {QEMU_ENTROPY_ID}))
   (then (return (if (result i32) (call $nxrs_entropy_available)
    (then (i32.const {QEMU_SIGNATURE})) (else (i32.const 0))))))
  (if (i32.eq (local.get $1) (i32.const {WDEV_RND}))
   (then (if (call $nxrs_entropy_available) (then (return (call $nxrs_random_word))))))
""")
    return prepend_function_body(module, "$88 (param $0 i32) (param $1 i32) (param $2 i32)", f"""
  (if {uart2}
   (then (call $nxrs_write_uart2 (local.get $0) {offset} (local.get $2)) (return)))
""")


def add_glue(source):
    source = insert_before(source, '        "./esp_emu_bg.js": import0,',
                           '        "nxrs_browser": browserDevices.imports,\n')
    source = insert_before(source, '        const ptr0 = passStringToWasm0(chip,',
                           '        browserDevices.reset();\n        wasm.nxrs_reset_uart2();\n')
    source = insert_before(source, '    /**\n     * Harts currently running', """
    uart2_input(data) {
        browserDevices.receive(data);
        wasm.nxrs_refresh_uart2();
    }

    uart2_output() {
        return browserDevices.transmit();
    }

    browser_device_stats() {
        return browserDevices.stats();
    }

    write_touch_word(address, word) {
        return wasm.nxrs_touch_word(this.__wbg_ptr, address, word) === 1;
    }

""")
    source = insert_before(source, '            wasm.wasmemulator_restart(retptr, this.__wbg_ptr);',
                           '            browserDevices.reset();\n            wasm.nxrs_reset_uart2();\n')
    return "import { browserDevices } from './browser_devices.js';\n\n" + source


def prepare(package, output):
    glue = package / "esp_emu.js"
    if package.resolve() == output.resolve():
        raise ValueError("choose a separate directory for the patched release")
    if hashlib.sha256(glue.read_bytes()).hexdigest() != GLUE_SHA256:
        raise ValueError("expected the unmodified ESP-EMU 1.2.0 browser glue")
    patched_glue = add_glue(glue.read_text())
    output.mkdir(parents=True, exist_ok=True)
    patch(package / "esp_emu_bg.wasm", output / "esp_emu_bg.wasm", add_devices)
    (output / "esp_emu.js").write_text(patched_glue)
    copyfile(Path(__file__).with_name("browser_devices.js"), output / "browser_devices.js")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="stock ESP-EMU 1.2.0 www/pkg directory")
    parser.add_argument("output", type=Path, help="directory for the patched browser package")
    args = parser.parse_args()
    prepare(args.package, args.output)
