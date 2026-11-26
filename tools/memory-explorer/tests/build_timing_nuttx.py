#!/usr/bin/env python3
"""Build the native timing witness in supplied disposable pinned NuttX trees.

This deliberately does not claim to build an NXRS application or product profile.
It installs one test application and resolves its Kconfig through normal NuttX
make targets, retaining the effective configuration, source identities and flags.
Put the ARM cross tools and kconfig-conf on PATH before invocation.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[3]


def pinned_revisions(root=ROOT):
    release = json.loads((root / "upstream/nuttx/release.json").read_text())
    pins = {}
    for name, component in (("nuttx", "nuttx"), ("apps", "nuttx-apps")):
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", ":external/" + component], text=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError(f"Invalid checked-in {component} source pin")
        pins[name] = revision
    if release.get("schema") != 1 or release.get("revision") != pins["nuttx"]:
        raise RuntimeError("NuttX release manifest does not match the checked-in source pin")
    return pins


def validate_configuration(text, settings):
    effective = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line[7:].split("=", 1)
            effective[key] = value
        elif match := re.fullmatch(r"# CONFIG_(\w+) is not set", line):
            effective[match[1]] = "n"
    checks = {}
    for key, requested in settings.items():
        # Kconfig omits invisible disabled symbols as well as writing explicit
        # "is not set" entries; neither generates an enabled CONFIG macro.
        actual = effective.get(key, "n")
        matches = requested == actual
        if not matches and requested not in ("y", "m", "n"):
            try:
                matches = int(requested, 0) == int(actual, 0)
            except ValueError:
                pass
        if not matches:
            raise RuntimeError(f"Required configuration did not resolve: {key} requested={requested} effective={actual}")
        checks[key] = {"requested": requested, "effective": actual,
                       "entry_present": key in effective}
    return checks


def source_identity(tree, revision):
    actual = subprocess.check_output(["git", "-C", str(tree), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise RuntimeError(f"Expected pinned source {revision}, found {actual}")
    dirty = subprocess.check_output(["git", "-C", str(tree), "diff", "--name-only", "HEAD", "--"], text=True)
    if dirty.strip():
        raise RuntimeError(f"Tracked source modifications are not admitted for this witness: {tree}\n{dirty}")
    untracked = subprocess.check_output(["git", "-C", str(tree), "ls-files", "--others", "--exclude-standard"], text=True)
    return {"revision": actual, "tracked_sources_clean": True,
            "untracked_paths_before_fixture_install": untracked.splitlines()}


def run(args):
    nuttx, apps, output = [getattr(args, name).resolve() for name in ("nuttx", "apps", "output")]
    pins = pinned_revisions()
    identities = {"nuttx": source_identity(nuttx, pins["nuttx"]), "apps": source_identity(apps, pins["apps"])}
    output.mkdir(parents=True, exist_ok=False)
    fixture = ROOT / "tests" / "nuttx-std" / "memory-timing"
    shutil.copytree(fixture, apps / "examples" / "memory_timing", dirs_exist_ok=True)
    settings = {name: "y" for name in (
        "DEBUG_SYMBOLS", "DEBUG_LINK_MAP", "SCHED_INSTRUMENTATION", "SCHED_INSTRUMENTATION_FILTER",
        "SCHED_INSTRUMENTATION_SWITCH", "SCHED_INSTRUMENTATION_IRQHANDLER", "SCHED_INSTRUMENTATION_DUMP",
        "DRIVERS_NOTE", "DRIVERS_NOTECTL", "DRIVERS_NOTERAM", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE",
        "EXAMPLES_MEMORY_TIMING", "SYSTEM_TRACE", "STACK_COLORATION", "SCHED_BACKTRACE", "UNWINDER_ARM")}
    settings.update({name: "n" for name in (
        "SMP", "ARCH_FPU", "HAVE_CXX", "HAVE_CXXINITIALIZE", "NET", "DEBUG_SCHED", "DEBUG_SCHED_ERROR",
        "TESTING_OSTEST", "TESTING_GETPRIME", "EXAMPLES_HELLO", "ARCH_PERF_EVENTS", "PERF_OVERFLOW_CORRECTION")})
    settings.update({"DRIVERS_NOTERAM_BUFSIZE": "32768", "DRIVERS_NOTE_TASKNAME_BUFSIZE": "4096",
                     "SCHED_INSTRUMENTATION_FILTER_DEFAULT_MODE": "0x0", "SYSTEM_TRACE_STACKSIZE": "8192",
                     "TASK_NAME_SIZE": "31", "MM_BACKTRACE": "8", "MM_BACKTRACE_DEFAULT": "y",
                     "EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK": "y" if args.qemu_timer_clock else "n"})
    commands = []

    def execute(command, log):
        commands.append(command)
        subprocess.run(command, cwd=nuttx, stdout=log, stderr=subprocess.STDOUT, check=True)

    with (output / "configure.log").open("w") as log:
        config = nuttx / ".config"
        if not config.exists():
            # The pinned configure.sh prepends TOPDIR to -a even for absolute
            # input; its documented contract requires a relative apps path.
            execute(["bash", "tools/configure.sh", "-l", "-a", os.path.relpath(apps, nuttx),
                     "mps2-an521:nsh"], log)
        elif "CONFIG_ARCH_BOARD_MPS2_AN521=y" not in config.read_text().splitlines():
            raise RuntimeError("The supplied build tree is configured for another board")
        original = config.read_text().splitlines()
        lines = [line for line in original if not any(line.startswith("CONFIG_" + key + "=") or
                 line == "# CONFIG_" + key + " is not set" for key in settings)]
        lines.extend("# CONFIG_" + key + " is not set" if value == "n" else "CONFIG_" + key + "=" + value
                     for key, value in settings.items())
        config.write_text("\n".join(lines) + "\n")
        execute(["make", "olddefconfig"], log)
        checks = validate_configuration(config.read_text(), settings)
        shutil.copy2(config, output / "resolved.config")
    command = ["make", "-j4"]
    if args.qemu_timer_clock:
        command.append("EXTRALINKCMDS=--wrap=up_perf_gettime --wrap=up_perf_getfreq --wrap=up_perf_convert")
    with (output / "build.log").open("w") as log:
        execute(command, log)
    shutil.copy2(nuttx / "nuttx", output / "nuttx")
    if (nuttx / "nuttx.map").is_file():
        shutil.copy2(nuttx / "nuttx.map", output / "nuttx.map")
    report = {"schema_version": 1, "workload": "native NuttX memory_timing qualification fixture",
              "nuttx_revision": pins["nuttx"], "apps_revision": pins["apps"],
              "source_identity": identities, "configuration_checks": checks,
              "qemu_cmsdk_clock_adapter": args.qemu_timer_clock, "commands": commands,
              "clock_contract": {"resource": "otherwise unused AN521 CMSDK APB timer1",
                                 "secure_address": 0x50001000, "frequency_hz": 20000000,
                                 "bits": 32, "wrap_period_ns": 214748364800,
                                 "epoch": "reset at fixture invocation", "irq_enabled": False,
                                 "scope": "explicit QEMU qualification only"} if args.qemu_timer_clock else None,
              "compiler": subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0],
              "fixture_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(fixture.iterdir()) if path.is_file()},
              "elf_sha256": hashlib.sha256((output / "nuttx").read_bytes()).hexdigest(),
              "config_sha256": hashlib.sha256((output / "resolved.config").read_bytes()).hexdigest()}
    (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"build": str(output / "build.json"), "elf_sha256": report["elf_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("nuttx", "apps", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--qemu-timer-clock", action="store_true")
    run(parser.parse_args())
