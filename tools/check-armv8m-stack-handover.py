#!/usr/bin/env python3
"""Check the compiled arm_initialize_stack handover without running the target.

This deliberately recognizes the short sequence in NuttX patch 0149, independent
of GCC's register allocation. It is not a general ARM control-flow verifier.
Changes to that sequence require review and an update to this contract. Passing
establishes instruction ordering, masking and branch preservation in one ELF;
it does not qualify stack addresses, interrupt delivery or runtime NMI behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import NamedTuple


SYMBOL = "arm_initialize_stack"
MAX_DISASSEMBLY_BYTES = 1024 * 1024
REGISTER_ALIASES = {"sb": "r9", "sl": "r10", "fp": "r11", "ip": "r12",
                    "lr": "r14", "sp": "r13", "pc": "r15"}
SPECIAL_WRITES = {"primask", "control", "msp", "psp", "msplim", "psplim"}
BRANCHES = {"b", "beq", "bne", "bcs", "bcc", "bhs", "blo", "bmi", "bpl",
            "bvs", "bvc", "bhi", "bls", "bge", "blt", "bgt", "ble", "bal",
            "bx", "cbz", "cbnz", "tbb", "tbh"}


class ContractError(ValueError):
    """The compiled function does not match the reviewed handover."""


class Instruction(NamedTuple):
    address: int
    size: int
    opcode: str
    operands: tuple[str, ...]


def _operand(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("#"):
        try:
            return f"#{int(value[1:], 0)}"
        except ValueError:
            pass
    return REGISTER_ALIASES.get(value, value)


def _instructions(text: str) -> tuple[int, list[Instruction]]:
    header = re.compile(r"^\s*([0-9a-fA-F]+) <([^>]+)>:\s*$")
    lines = text.splitlines()
    matches = [(i, match) for i, line in enumerate(lines)
               if (match := header.fullmatch(line)) and match[2] == SYMBOL]
    if len(matches) != 1:
        raise ContractError(f"expected one {SYMBOL} symbol, found {len(matches)}")
    index, symbol = matches[0]
    result = []
    for line in lines[index + 1:]:
        if header.fullmatch(line) or line.startswith("Disassembly of section "):
            break
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([0-9a-fA-F]+):\s+(.+)", line)
        if not match:
            raise ContractError(f"unrecognized objdump line in {SYMBOL}: {line.strip()}")
        fields = re.split(r"\s+", match[2].strip())
        encoding = []
        while fields and re.fullmatch(r"(?:[0-9a-fA-F]{2}|[0-9a-fA-F]{4}|[0-9a-fA-F]{8})", fields[0]):
            encoding.append(fields.pop(0))
        size = sum(len(part) // 2 for part in encoding)
        if size not in (2, 4) or not fields:
            raise ContractError(f"expected a decoded Thumb instruction at 0x{int(match[1], 16):x}")
        opcode = re.sub(r"\.(?:w|n)$", "", fields.pop(0).lower())
        operands = re.split(r"[;@]", " ".join(fields), maxsplit=1)[0].strip()
        result.append(Instruction(int(match[1], 16), size, opcode,
                                  tuple(_operand(part) for part in operands.split(",")) if operands else ()))
    if not result:
        raise ContractError(f"no instructions decoded for {SYMBOL}")
    return int(symbol[1], 16), result


def _register(value: str) -> str:
    if not re.fullmatch(r"r(?:[0-9]|1[0-2]|14)", value):
        raise ContractError(f"expected a general register other than SP/PC, got {value!r}")
    return value


def _branch_target(instruction: Instruction) -> int:
    if len(instruction.operands) != 1:
        raise ContractError("expected one direct BNE target")
    match = re.fullmatch(r"(?:0x)?([0-9a-f]+)(?:\s+<[^>]+>)?", instruction.operands[0])
    if not match:
        raise ContractError(f"unrecognized BNE target: {instruction.operands[0]!r}")
    return int(match[1], 16)


def check_disassembly(text: str, hardware_limits: bool) -> dict:
    """Check GNU objdump text; synthetic host tests call this pure function."""
    symbol_address, instructions = _instructions(text)
    saves = [i for i, ins in enumerate(instructions)
             if ins.opcode == "mrs" and len(ins.operands) == 2 and ins.operands[1] == "primask"]
    if len(saves) != 1:
        raise ContractError(f"expected one PRIMASK save, found {len(saves)}")
    start = saves[0]
    cursor = start

    def expect(opcode: str | tuple[str, ...], operands: tuple[str | None, ...]) -> Instruction:
        nonlocal cursor
        choices = (opcode,) if isinstance(opcode, str) else opcode
        if cursor == len(instructions):
            raise ContractError(f"handover ended before expected {choices[0]}")
        ins = instructions[cursor]
        if ins.opcode not in choices or len(ins.operands) != len(operands) or any(
                required is not None and actual != required for actual, required in zip(ins.operands, operands)):
            wanted = f"{choices[0]} " + ", ".join(value or "<register>" for value in operands)
            actual = f"{ins.opcode} " + ", ".join(ins.operands)
            raise ContractError(f"0x{ins.address:x}: expected {wanted}; got {actual}")
        cursor += 1
        return ins

    def isb() -> Instruction:
        ins = expect("isb", (None,))
        if ins.operands[0] not in {"sy", "#15"}:
            raise ContractError(f"0x{ins.address:x}: expected ISB SY")
        return ins

    mask = _register(expect("mrs", (None, "primask")).operands[0])
    masked = expect("cpsid", ("i",))
    control = _register(expect("mrs", (None, "control")).operands[0])
    expect("tst", (control, "#2"))
    branch = expect("bne", (None,))
    temp = _register(expect("mrs", (None, "msp")).operands[0])
    expect("msr", ("psp", temp))
    if hardware_limits:
        expect("mrs", (temp, "msplim"))
        expect("msr", ("psplim", temp))
    if cursor == len(instructions):
        raise ContractError("missing common MSP initialization")
    common = instructions[cursor].address
    if _branch_target(branch) != common:
        raise ContractError(f"BNE must skip only the PSP copy and reach common initialization at 0x{common:x}")
    if hardware_limits:
        expect(("mov", "movs"), (temp, "#0"))
        expect("msr", ("msplim", temp))
    msp = expect("msr", ("msp", None))
    stack = _register(msp.operands[1])
    limit = None
    if hardware_limits:
        limit = _register(expect("msr", ("msplim", None)).operands[1])
    isb()
    expect(("orr", "orrs"), (control, control, "#2"))
    select = expect("msr", ("control", control))
    isb()
    restore = expect("msr", ("primask", mask))

    roles = {"saved_primask": mask, "saved_control": control, "temporary": temp,
             "irq_stack_top": stack}
    if limit is not None:
        roles["irq_stack_limit"] = limit
    if len(set(roles.values())) != len(roles):
        raise ContractError(f"handover register roles must be independent: {roles}")
    block = instructions[start:cursor]
    for previous, following in zip(block, block[1:]):
        if previous.address + previous.size != following.address:
            raise ContractError(f"noncontiguous handover instructions after 0x{previous.address:x}")

    # Only a straight-line compiler prologue (possibly calling a helper) is
    # admitted. Do not accept a branch that bypasses the PRIMASK save or enters
    # the inline block with unrelated values. Normal return epilogues are fine.
    for index, ins in enumerate(instructions):
        if start <= index < cursor:
            continue
        if (ins.opcode == "msr" and ins.operands and ins.operands[0] in SPECIAL_WRITES
                or ins.opcode in {"cpsid", "cpsie"}):
            raise ContractError(f"stack/control/mask write outside checked handover at 0x{ins.address:x}")
        pc_destination = bool(ins.operands and ins.operands[0] == "r15")
        pc_load = (ins.opcode == "pop" or ins.opcode.startswith("ldm")) and any(
            re.search(r"\b(?:pc|r15)\b", operand) for operand in ins.operands)
        if index < start and (ins.opcode in BRANCHES or re.fullmatch(r"it[te]{0,3}", ins.opcode)
                              or pc_destination or pc_load):
            raise ContractError(f"unsupported control flow before handover at 0x{ins.address:x}")
        if ins.opcode in BRANCHES | {"bl", "blx"} and ins.operands:
            # CBZ/CBNZ carry the destination in their last operand.
            destination = re.match(r"(?:0x)?([0-9a-f]+)(?:\s+<|$)", ins.operands[-1])
            if destination and block[0].address <= int(destination[1], 16) < restore.address + restore.size:
                raise ContractError(f"branch into checked handover at 0x{ins.address:x}")

    return {
        "schema_version": 1,
        "kind": "armv8m-stack-handover-static",
        "passed": True,
        "runtime_qualified": False,
        "symbol": SYMBOL,
        "symbol_address": f"0x{symbol_address:x}",
        "hardware_limits": "enabled" if hardware_limits else "disabled",
        "handover_start": f"0x{block[0].address:x}",
        "handover_end_exclusive": f"0x{restore.address + restore.size:x}",
        "interrupt_mask": f"0x{masked.address:x}",
        "already_psp_target": f"0x{common:x}",
        "msp_install": f"0x{msp.address:x}",
        "control_select": f"0x{select.address:x}",
        "registers": roles,
        "instruction_count": len(block),
        "checks": [
            "saved PRIMASK restored unchanged after the handover",
            "configurable interrupts masked before stack-register writes",
            "already-PSP branch preserves PSP and PSPLIM",
            "MSP installed before CONTROL selects PSP",
            "immediate ISB after CONTROL write",
            "no memory access or call inside the handover",
        ] + (["old limit copied, MSP limit cleared and new limit installed before CONTROL"]
             if hardware_limits else []),
        "limitations": "Static instruction contract only; stack bounds, runtime IRQ/NMI behavior and callers are not qualified.",
    }


def inspect_elf(elf: Path, objdump: str, hardware_limits: bool) -> dict:
    """Read an ARM ELF and invoke only its disassembler, never the image."""
    with elf.open("rb") as source:
        header = source.read(20)
        if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 1 or header[5] not in (1, 2):
            raise ContractError("expected an ELF32 ARM image")
        order = "little" if header[5] == 1 else "big"
        if int.from_bytes(header[18:20], order) != 40:
            raise ContractError("expected an ELF32 ARM image (e_machine=EM_ARM)")
        digest = hashlib.sha256(header)
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    command = [objdump, "-d", f"--disassemble={SYMBOL}", str(elf)]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=30, check=False)
        if stdout.tell() > MAX_DISASSEMBLY_BYTES:
            raise ContractError("objdump output exceeds the 1 MiB single-function bound")
        stdout.seek(0)
        disassembly = stdout.read().decode("utf-8", errors="strict")
        if process.returncode:
            stderr.seek(0)
            detail = stderr.read(4096).decode("utf-8", errors="replace").strip()
            raise ContractError(f"objdump failed ({process.returncode}): {detail}")
    result = check_disassembly(disassembly, hardware_limits)
    result.update({"elf": str(elf), "elf_sha256": digest.hexdigest(),
                   "disassembly_sha256": hashlib.sha256(disassembly.encode()).hexdigest(),
                   "objdump_command": command})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True, help="built ELF32 ARM image (read only)")
    parser.add_argument("--objdump", default="arm-none-eabi-objdump", help="GNU ARM objdump executable")
    parser.add_argument("--hardware-limits", choices=("enabled", "disabled"), required=True,
                        help="explicit expected CONFIG_ARMV8M_STACKCHECK_HARDWARE setting")
    parser.add_argument("--output", type=Path, help="also write the successful JSON receipt here")
    args = parser.parse_args()
    try:
        if args.output and args.output.resolve() == args.elf.resolve():
            raise ContractError("receipt output must differ from the input ELF")
        result = inspect_elf(args.elf.resolve(), args.objdump, args.hardware_limits == "enabled")
        receipt = json.dumps(result, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(receipt, encoding="utf-8")
        print(receipt, end="")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"stack handover check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
