#!/usr/bin/env python3
"""Build native allocator outcome evidence using the exact production patch.

Use disposable pinned NuttX/apps trees, after retaining any earlier unpatched
timing/replay witnesses. The normal repository submodules are never modified.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from build_timing_nuttx import (APPS_REVISION, NUTTX_REVISION, source_identity,
                                validate_configuration)


PATCH = "upstream/nuttx/patches/0148-trace-heap-allocation-requests.patch"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_paths(text):
    paths = re.findall(r"^diff --git a/(\S+) b/(\S+)$", text, re.MULTILINE)
    if not paths or any(first != second or Path(first).is_absolute() or
                        ".." in Path(first).parts for first, second in paths):
        raise RuntimeError("Production instrumentation patch has unsupported paths")
    result = [second for _, second in paths]
    if len(set(result)) != len(result):
        raise RuntimeError("Production instrumentation patch repeats a source path")
    return sorted(result)


def validate_apps_directory(nuttx, apps, configuration):
    """Admit the literal APPDIR that pinned tools/Unix.mk will actually use.

    Unix.mk resolves CONFIG_APPS_DIR from the NuttX working directory and
    requires a readable Makefile. An unset/blank variable defaults to ../apps.
    Do not evaluate make or shell expansions while checking source identity.
    """
    values = [line.partition("=")[2].strip() for line in configuration.splitlines()
              if line.startswith("CONFIG_APPS_DIR=")]
    if len(values) > 1:
        raise RuntimeError("Ambiguous CONFIG_APPS_DIR in the native configuration")
    value = values[0] if values else ""
    if not value:
        value = "../apps"
    elif value.startswith('"'):
        if "\\" in value:
            raise RuntimeError("CONFIG_APPS_DIR must be a supported literal path")
        try:
            value = json.loads(value)
        except ValueError as error:
            raise RuntimeError("Malformed CONFIG_APPS_DIR string") from error
    # These characters have nonliteral make/shell meaning in Unix.mk. Its
    # realpath also splits whitespace, so paths requiring quoting are refused.
    if not value or any(character.isspace() or character in "$`\\\"';|&<>(){}[]*?!#~"
                        for character in value):
        raise RuntimeError("CONFIG_APPS_DIR must be a supported literal path")
    configured = (nuttx / value).resolve()
    if configured != apps.resolve():
        raise RuntimeError(f"The configured apps directory {configured} differs from --apps {apps.resolve()}")
    makefile = configured / "Makefile"
    if not makefile.is_file() or not os.access(makefile, os.R_OK):
        raise RuntimeError("The configured apps directory has no readable Makefile")
    return configured


def run(args):
    nuttx, apps, output = [getattr(args, name).resolve() for name in ("nuttx", "apps", "output")]
    repo = Path(__file__).resolve().parents[3]
    if nuttx == (repo / "external/nuttx").resolve() or apps == (repo / "external/nuttx-apps").resolve():
        raise RuntimeError("Use disposable source clones, not the repository submodules")
    config = nuttx / ".config"
    if config.exists():
        configuration = config.read_text()
        if "CONFIG_ARCH_BOARD_MPS2_AN521=y" not in configuration.splitlines():
            raise RuntimeError("The disposable tree is configured for another board")
        validate_apps_directory(nuttx, apps, configuration)
    identities = {"nuttx": source_identity(nuttx, NUTTX_REVISION),
                  "apps": source_identity(apps, APPS_REVISION)}
    patch = repo / PATCH
    paths = patch_paths(patch.read_text())
    original_hashes = {path: sha256(nuttx / path) for path in paths if (nuttx / path).is_file()}
    output.mkdir(parents=True, exist_ok=False)
    commands = []

    def execute(command, log):
        commands.append(command)
        subprocess.run(command, cwd=nuttx, stdout=log, stderr=subprocess.STDOUT, check=True)

    with (output / "patch.log").open("w") as log:
        execute(["git", "apply", "--check", str(patch)], log)
        execute(["git", "apply", str(patch)], log)
    changed = subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--"], cwd=nuttx, text=True).splitlines()
    # New source files are admitted only when the retained patch names them.
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"],
                                        cwd=nuttx, text=True).splitlines()
    introduced = sorted(set(untracked) - set(identities["nuttx"]["untracked_paths_before_fixture_install"]))
    if sorted(changed + introduced) != paths:
        raise RuntimeError("Patched source changes differ from the production patch inventory")
    patched_hashes = {path: sha256(nuttx / path) for path in paths}

    fixture = repo / "tests/nuttx-std/heap-requests"
    installed = apps / "examples/heap_requests"
    shutil.copytree(fixture, installed, dirs_exist_ok=True)
    clock = repo / "tests/nuttx-std/memory-timing/qemu_timer_clock.c"
    shutil.copy2(clock, installed / clock.name)
    settings = {name: "y" for name in (
        "DEBUG_SYMBOLS", "DEBUG_LINK_MAP", "DEBUG_NOOPT", "LTO_NONE", "MM_DEFAULT_MANAGER",
        "BUILD_FLAT", "EXAMPLES_HEAP_REQUESTS", "MM_ALLOC_REQUEST_TRACE", "SCHED_INSTRUMENTATION",
        "SCHED_INSTRUMENTATION_FILTER", "SCHED_INSTRUMENTATION_HEAP", "SCHED_INSTRUMENTATION_DUMP",
        "DRIVERS_NOTE", "DRIVERS_NOTECTL", "DRIVERS_NOTERAM", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE",
        "STACK_COLORATION", "SCHED_BACKTRACE", "UNWINDER_ARM")}
    settings.update({name: "n" for name in (
        "SMP", "ARCH_FPU", "HAVE_CXX", "HAVE_CXXINITIALIZE", "NET", "DEBUG_SCHED", "DEBUG_SCHED_ERROR",
        "DEBUG_MM", "DEBUG_MM_ERROR", "MM_PANIC_ON_FAILURE", "TESTING_OSTEST", "TESTING_GETPRIME",
        "EXAMPLES_HELLO", "EXAMPLES_MEMORY_TIMING", "EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK",
        "EXAMPLES_HEAP_REPLAY", "ARCH_PERF_EVENTS", "PERF_OVERFLOW_CORRECTION", "MM_HEAP_MEMPOOL",
        "MM_KERNEL_HEAP", "MM_TLSF_MANAGER", "MM_KASAN", "MM_FILL_ALLOCATIONS",
        "SCHED_INSTRUMENTATION_SWITCH", "SCHED_INSTRUMENTATION_IRQHANDLER", "SCHED_INSTRUMENTATION_SYSCALL")})
    settings.update({"DRIVERS_NOTERAM_BUFSIZE": "32768", "DRIVERS_NOTE_TASKNAME_BUFSIZE": "4096",
                     "SCHED_INSTRUMENTATION_FILTER_DEFAULT_MODE": "0x0", "TASK_NAME_SIZE": "31",
                     "MM_REGIONS": "1", "MM_BACKTRACE": "8", "MM_BACKTRACE_DEFAULT": "y",
                     "MM_DEFAULT_ALIGNMENT": "8", "MM_NODE_GUARDSIZE": "0", "MM_FREE_DELAYCOUNT_MAX": "0"})
    with (output / "configure.log").open("w") as log:
        if not config.exists():
            execute(["bash", "tools/configure.sh", "-l", "-a", os.path.relpath(apps, nuttx), "mps2-an521:nsh"], log)
        if "CONFIG_ARCH_BOARD_MPS2_AN521=y" not in config.read_text().splitlines():
            raise RuntimeError("The disposable tree is configured for another board")
        validate_apps_directory(nuttx, apps, config.read_text())
        execute(["make", "-C", str(apps / "examples"), "APPDIR=" + str(apps),
                 "TOPDIR=" + str(nuttx), "preconfig"], log)
        lines = [line for line in config.read_text().splitlines() if not any(
            line.startswith("CONFIG_" + key + "=") or line == "# CONFIG_" + key + " is not set" for key in settings)]
        lines.extend("# CONFIG_" + key + " is not set" if value == "n" else "CONFIG_" + key + "=" + value
                     for key, value in settings.items())
        config.write_text("\n".join(lines) + "\n")
        execute(["make", "olddefconfig"], log)
        configured_apps = validate_apps_directory(nuttx, apps, config.read_text())
        checks = validate_configuration(config.read_text(), settings)
        shutil.copy2(config, output / "resolved.config")
    with (output / "build.log").open("w") as log:
        execute(["make", "-j4", "EXTRALINKCMDS=--wrap=up_perf_gettime --wrap=up_perf_getfreq --wrap=up_perf_convert"], log)
    for name in ("nuttx", "nuttx.map"):
        shutil.copy2(nuttx / name, output / name)
    disassembly = subprocess.check_output(["arm-none-eabi-objdump", "-d", str(output / "nuttx")], text=True)
    (output / "disassembly.txt").write_text(disassembly)
    symbols = subprocess.check_output(["arm-none-eabi-nm", "-S", "--size-sort", str(output / "nuttx")], text=True)
    (output / "symbols.txt").write_text(symbols)
    owned = {"g_mm_request_id", "mm_request_begin", "mm_request_end", "mm_alloc_requests",
             "sched_note_heap_request", "sched_note_heap_result"}
    linked = []
    for line in symbols.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[3] in owned:
            linked.append({"name": parts[3], "address": int(parts[0], 16),
                           "size": int(parts[1], 16), "kind": parts[2]})
    if {item["name"] for item in linked} != owned or len(linked) != len(owned):
        raise RuntimeError("Required production recorder symbols are missing or ambiguous")
    if next(item["size"] for item in linked if item["name"] == "g_mm_request_id") != 8:
        raise RuntimeError("Unexpected boot-global recorder ID storage ABI")
    report = {"schema_version": 1, "workload": "native allocator request/result qualification",
        "nuttx_revision": NUTTX_REVISION, "apps_revision": APPS_REVISION,
        "source_identity_before_patch": identities, "configuration_checks": checks, "commands": commands,
        "configured_apps_directory": str(configured_apps),
        "production_patch": {"path": PATCH, "sha256": sha256(patch), "changed_paths": paths,
                             "original_sources": original_hashes, "patched_sources": patched_hashes},
        "fixture_sha256": {path.name: sha256(path) for path in sorted(fixture.iterdir()) if path.is_file()},
        "shared_clock_sha256": sha256(clock), "elf_sha256": sha256(output / "nuttx"),
        "config_sha256": sha256(output / "resolved.config"),
        "compiler": subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0],
        "linked_recorder_symbols": linked,
        "note_buffer_bytes": int(settings["DRIVERS_NOTERAM_BUFSIZE"]),
        "static_arena_bytes": 65536, "task_stack_reservation_bytes": 8192,
        "clock_contract": {"clock": "qemu-an521-cmsdk-timer1-20mhz", "frequency_hz": 20000000,
                           "bits": 32, "epoch": "fixture invocation", "irq_enabled": False},
        "limits": ["Private fixture heap using production instrumentation; not product topology replay",
                   "Explicit single-core default-allocator admission; no pools or SMP qualification",
                   "Arena and stack values are reservations, not target overhead or high-water measurements",
                   "Recorder symbol sizes exclude hook-site code, alignment and shared helpers; not a whole-firmware delta"]}
    (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"build": str(output / "build.json"), "elf_sha256": report["elf_sha256"],
                      "production_patch_sha256": report["production_patch"]["sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("nuttx", "apps", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(parser.parse_args())
