#!/usr/bin/env python3
"""Supply ESP32-S3 UART_CLK_CONF readback in the ESP-EMU 1.2.0 browser module."""

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory


RELEASE_SHA256 = "aa821b374753aa967163956c3554309bb265621bc3eace8a94631058f7c67a94"
UART_CLK_CONF = (0x60000078, 0x60010078, 0x6002E078)
# UART clock enables, XTAL source and integral divider reset values.
RESET = 0x03701000


def prepend_function_body(module, signature, instructions):
    header = re.compile(rf"^ \(func {re.escape(signature)}\n(?:  \(local [^\n]*\n)*", re.M)
    module, count = header.subn(lambda match: match.group() + instructions, module)
    if count != 1:
        raise ValueError(f"expected a single ESP-EMU function {signature}")
    return module


def patch(source, output, extend=None):
    if source.resolve() == output.resolve():
        raise ValueError("choose a separate output for the patched module")
    if hashlib.sha256(source.read_bytes()).hexdigest() != RELEASE_SHA256:
        raise ValueError("expected the unmodified ESP-EMU 1.2.0 browser module")

    with TemporaryDirectory(prefix="esp-emu-uart-") as directory:
        wat = Path(directory) / "esp-emu.wat"
        subprocess.run(["wasm-dis", str(source), "-o", str(wat)], check=True)
        module = wat.read_text()
        declarations = []
        reads = []
        writes = []
        for uart, address in enumerate(UART_CLK_CONF):
            register = f"$uart{uart}_clk_conf"
            condition = f"(i32.eq (local.get $1) (i32.const {address}))"
            declarations.append(f" (global {register} (mut i32) (i32.const {RESET}))\n")
            reads.append(f"  (if {condition} (then (return (global.get {register}))))\n")
            writes.append(f"  (if {condition} (then (global.set {register} (local.get $2))))\n")

        module = module.replace(" (global ", "".join(declarations) + " (global ", 1)
        # These two dispatchers take the machine pointer and guest MMIO address.
        # The write dispatcher also takes the value and retains its original work.
        for dispatch, instructions in [
            ("$105 (param $0 i32) (param $1 i32) (result i32)", reads),
            ("$88 (param $0 i32) (param $1 i32) (param $2 i32)", writes),
        ]:
            module = prepend_function_body(module, dispatch, "".join(instructions))

        wat.write_text(extend(module) if extend else module)
        subprocess.run(["wasm-as", str(wat), "--all-features", "-o", str(output)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    patch(args.source, args.output)
