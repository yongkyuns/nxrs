#!/usr/bin/env python3
"""Compile and inspect the prepared ARMv8-M startup source without executing it.

Use a normal cargo nxrs firmware build's configured NuttX tree. Keep its source
and configuration unchanged; derive the real compile command from NuttX make,
then compile separate objects at -O0/-Os with hardware stack limits on/off.
This checks compiler output, not runtime exception behavior or hardware timing.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    nuttx = args.nuttx.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    architecture = nuttx / "arch/arm/src"
    source = architecture / "armv8-m/arm_initialstate.c"
    configuration = nuttx / "include/nuttx/config.h"
    inputs = {str(path): digest(path) for path in (source, configuration)}
    # --always-make prints the compile command even after the normal build.
    # --just-print leaves all prepared sources and native objects untouched.
    query = ["make", "--no-print-directory", "--just-print", "--always-make",
             "-C", str(architecture), "TOPDIR=" + str(nuttx), "V=1",
             "arm_initialstate.o"]
    dry_run = subprocess.run(query, text=True, capture_output=True,
                             check=True, timeout=30)
    (output / "compile-command.log").write_text(dry_run.stdout + dry_run.stderr)
    candidates = []
    for line in dry_run.stdout.splitlines():
        words = shlex.split(line)
        if "-c" in words and "-o" in words and any(
                word.endswith("armv8-m/arm_initialstate.c") for word in words):
            candidates.append(words)
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one native arm_initialstate compile command")
    command = candidates[0]
    if any(word in (";", "&&", "||", "|") for word in command):
        raise RuntimeError("Native compile command must be one executable invocation")
    compiler = subprocess.check_output([command[0], "--version"], text=True,
                                       timeout=10).splitlines()[0]
    checker = Path(__file__).resolve().parents[2] / "tools/check-armv8m-stack-handover.py"
    variants = []
    for limits in ("enabled", "disabled"):
        override = output / ("limits-" + limits + ".h")
        override.write_text('#include <nuttx/config.h>\n'
                            '#undef CONFIG_ARMV8M_STACKCHECK_HARDWARE\n' +
                            ('#define CONFIG_ARMV8M_STACKCHECK_HARDWARE 1\n'
                             if limits == "enabled" else ""))
        for optimization in ("-O0", "-Os"):
            name = optimization[1:] + "-limits-" + limits
            obj = output / (name + ".o")
            compile_command = list(command)
            compile_command[compile_command.index("-o") + 1] = str(obj)
            compile_command.extend([optimization, "-include", str(override)])
            with (output / (name + ".log")).open("w") as log:
                subprocess.run(compile_command, cwd=architecture, stdout=log,
                               stderr=subprocess.STDOUT, check=True, timeout=60)
            receipt = output / (name + ".json")
            subprocess.run([sys.executable, str(checker), "--elf", str(obj),
                            "--objdump", args.objdump, "--hardware-limits", limits,
                            "--output", str(receipt)], check=True, timeout=30)
            variants.append({"name": name, "command": compile_command,
                             "object_sha256": digest(obj),
                             "inspection": json.loads(receipt.read_text())})
    if any(digest(Path(path)) != value for path, value in inputs.items()):
        raise RuntimeError("Prepared startup source or configuration changed during inspection")
    report = {"schema_version": 1, "compiler": compiler, "source_sha256": inputs,
              "compile_command_query": query, "variants": variants,
              "prepared_sources_unchanged": True, "runtime_qualified": False}
    (output / "variants.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"variants": len(variants), "report": str(output / "variants.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nuttx", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objdump", default="arm-none-eabi-objdump")
    run(parser.parse_args())
