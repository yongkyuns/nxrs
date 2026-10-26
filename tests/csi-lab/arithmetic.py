"""Enforce the separate-product arithmetic boundary in actual Xtensa code."""

from pathlib import Path
import re
import subprocess


def qualify(disassembly, minimum_products=4):
    if re.search(r"\b(?:madd|msub|nmadd|nmsub)\.s\b", disassembly):
        raise ValueError("Pearson products contracted into fused accumulation")
    products = len(re.findall(r"\bmul\.s\b", disassembly))
    if products < minimum_products:
        raise ValueError("missing separate Pearson product instructions")
    return {
        "fused_accumulations": 0,
        "separate_product_instructions": products,
        "scope": "Compiler witness for the Pearson routine only; exact numerical/complete-policy execution remains required.",
    }


def inspect_routine(elf_path, objdump, prefix, minimum_products):
    from elftools.elf.elffile import ELFFile

    with Path(elf_path).open("rb") as stream:
        elf = ELFFile(stream)
        symbols = [
            s
            for s in elf.get_section_by_name(".symtab").iter_symbols()
            if s.name.startswith(prefix) and s["st_size"] > 0
        ]
        if len(symbols) != 1:
            raise ValueError("missing/ambiguous Pearson target symbol")
        symbol = symbols[0]
        start = symbol["st_value"]
        end = start + symbol["st_size"]
    assembly = subprocess.check_output(
        [
            str(objdump),
            "-d",
            f"--start-address={start}",
            f"--stop-address={end}",
            str(elf_path),
        ],
        text=True,
    )
    receipt = qualify(assembly, minimum_products)
    receipt["symbol"] = symbol.name
    return receipt


def inspect(elf_path, objdump):
    receipt = inspect_routine(
        elf_path, objdump, "_ZN12csi_lab_core8temporal8distance", 4
    )
    receipt["threshold_fit"] = inspect_routine(
        elf_path, objdump, "_ZN12csi_lab_core6motion6Motion13begin_holdout", 2
    )
    return receipt
