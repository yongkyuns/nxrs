#!/usr/bin/env python3
"""Observe ordinary startup of the corrected, source-bound native fixture.

No debugger, register edits or interrupt injection is used. A finite passing
window is reported as an observation, not as proof that every boot is safe.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("build", "comparison", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    helper = Path(__file__).with_name("diagnose-retained-arm-boot.py")
    spec = importlib.util.spec_from_file_location("retained_arm_boot", helper)
    diagnostic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostic)
    build = json.loads((args.build / "build.json").read_text())
    comparison = json.loads(args.comparison.read_text())
    image, config = args.build / "nuttx", args.build / "resolved.config"
    if (digest(image) != build["elf_sha256"] or digest(image) != comparison["rebuilt_elf_sha256"] or
            digest(config) != build["config_sha256"] or digest(config) != comparison["rebuilt_config_sha256"] or
            digest(args.build / "build.json") != comparison["build_receipt_sha256"] or
            comparison["atomic_stack_patch"] != build["atomic_stack_patch"] or
            not comparison["enabled_configuration_identical"]):
        raise RuntimeError("Corrected image/configuration/source admission differs")
    qemu = shutil.which("qemu-system-arm")
    if qemu is None:
        raise RuntimeError("QEMU executable is unavailable")
    version = subprocess.check_output([qemu, "--version"], text=True).splitlines()[0]
    if (version != diagnostic.QEMU_VERSION or
            digest(Path(qemu)) != "11d411dcfc21503acdd005fde7db36745adb1004ff7c44d0068d58e203f4a90e"):
        raise RuntimeError("Emulator differs from the reproduced startup failure")
    command = [qemu, "-machine", "mps2-an521", "-kernel", str(image.resolve()),
               "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    report = {"purpose": "Ordinary startup observation after the native stack correction",
              "elf_sha256": digest(image), "config_sha256": digest(config),
              "comparison_sha256": digest(args.comparison), "atomic_stack_patch": build["atomic_stack_patch"],
              "qemu_version": version, "qemu_sha256": digest(Path(qemu)),
              "helper_sha256": digest(helper), "maximum_boots": 1000,
              "host_deadline_seconds": 180, "debugger_used": False,
              "workload_commands_sent": 0, "qemu_trace": "int,guest_errors", "boots": []}
    deadline = time.monotonic() + 180
    for index in range(1, 1001):
        if time.monotonic() >= deadline:
            raise RuntimeError("Startup observation window exhausted before all boots completed")
        directory = args.output / f"boot-{index:03d}"
        directory.mkdir()
        result = diagnostic.boot(command, directory, deadline)
        report["boots"].append(result)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"boot {index}: {result['status']} ({result['console_bytes']} bytes)", flush=True)
        if result["status"] != "prompt_observed":
            print((directory / "console.log").read_text(errors="replace"), flush=True)
            print((directory / "qemu-interrupts.log").read_text(errors="replace")[-65536:], flush=True)
            raise SystemExit(1)
        if sum(item["console_bytes"] + item["interrupt_trace_bytes"] for item in report["boots"]) >= 32 * 1024 * 1024:
            raise RuntimeError("Startup observation total output bound reached")
    print("All 1000 ordinary boots reached the prompt; no assertion in this observation window.")


if __name__ == "__main__":
    main()
