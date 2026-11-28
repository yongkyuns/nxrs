#!/usr/bin/env python3
"""Build an isolated native heap witness in supplied disposable NuttX trees.

The exact ARM startup patch is applied temporarily and its source restored.
This fixture build is deliberately separate from the production profile and
preserves source/config/ELF receipts.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from build_timing_nuttx import (APPS_REVISION, NUTTX_REVISION, atomic_stack_transition,
                                source_identity, validate_configuration)


WRAPPERS = ("mm_lock", "mm_unlock", "up_perf_gettime", "up_perf_getfreq", "up_perf_convert")
ALLOCATOR_SOURCES = ("mm_malloc.c", "mm_free.c", "mm_realloc.c", "mm_memalign.c",
                     "mm_lock.c", "mm_shrinkchunk.c", "mm.h", "mm_initialize.c")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hook_coverage(disassembly):
    """Check actual linked destinations, not merely wrapper symbol presence.

    This is coupled to a clean pinned source/configuration audit: all admitted
    geometry changes are enclosed by the calls below. Initialization happens
    before activation; extension/destruction and pools are outside admission.
    """
    functions = {}
    callers = defaultdict(Counter)
    current = None
    for line in disassembly.splitlines():
        match = re.fullmatch(r"[0-9a-f]+ <([^>]+)>:", line)
        if match:
            current = match[1]
            functions[current] = []
        elif current is not None:
            functions[current].append(line)
            call = re.search(r"\b(?:bl|b)(?:\.w)?\s+[0-9a-f]+ <(mm_[^>]+)>", line)
            if call:
                callers[call[1]][current] += 1
    required = {"mm_malloc": (1, 1), "mm_delayfree": (1, 2),
                "mm_realloc": (1, 3), "mm_memalign": (1, 1)}
    checks = {}
    for name, (locks, unlocks) in required.items():
        body = "\n".join(functions.get(name, []))
        actual = [len(re.findall(r"\b(?:bl|b)(?:\.w)?\s+[0-9a-f]+ <__wrap_" + target + r">", body))
                  for target in ("mm_lock", "mm_unlock")]
        if actual != [locks, unlocks]:
            raise RuntimeError(f"Unexpected final ELF hook coverage for {name}: {actual}, expected {[locks, unlocks]}")
        checks[name] = {"wrapped_lock_calls": locks, "wrapped_unlock_calls": unlocks}
    direct = []
    for name, lines in functions.items():
        if name in ("__wrap_mm_lock", "__wrap_mm_unlock"):
            continue
        for line in lines:
            if re.search(r"\b(?:bl|b)(?:\.w)?\s+[0-9a-f]+ <mm_(?:un)?lock>", line):
                direct.append({"function": name, "instruction": line.strip()})
    if direct:
        raise RuntimeError(f"Unwrapped allocator mutex calls in final ELF: {direct}")
    for name in ("__wrap_mm_lock", "__wrap_mm_unlock", "replay_probe_stop", "heap_replay_main"):
        if name not in functions:
            raise RuntimeError(f"Required retained symbol missing: {name}")
    for target in ("mm_lock", "mm_unlock"):
        body = "\n".join(functions["__wrap_" + target])
        if len(re.findall(r"\b(?:bl|b)(?:\.w)?\s+[0-9a-f]+ <" + target + r">", body)) != 1:
            raise RuntimeError("Wrapper does not invoke exactly one real " + target)
    helpers = {
        "mm_addfreechunk": {"mm_addregion": 1, "mm_delayfree": 1, "mm_malloc": 1,
                            "mm_realloc": 2, "mm_shrinkchunk": 2, "mm_memalign": 1},
        "mm_shrinkchunk": {"mm_realloc": 1, "mm_memalign": 1},
        "mm_addregion": {"mm_initialize_heap": 1},
        "mm_uninitialize": {"heap_replay_main": 1},
    }
    for name, expected in helpers.items():
        if dict(callers[name]) != expected:
            raise RuntimeError(f"Unexpected geometry helper callers for {name}: {dict(callers[name])}")
    if callers["mm_extend"]:
        raise RuntimeError("Heap extension is not admitted during the isolated fixture")
    return {"functions": checks, "unwrapped_mutex_calls": direct, "geometry_helper_callers": helpers,
            "scope": "Pinned default allocator, single fixture-owned fixed heap; no pools or region lifecycle during capture"}


def run(args):
    nuttx, apps, output = [getattr(args, name).resolve() for name in ("nuttx", "apps", "output")]
    identities = {"nuttx": source_identity(nuttx, NUTTX_REVISION),
                  "apps": source_identity(apps, APPS_REVISION)}
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[3]
    fixture = repo / "tests/nuttx-std/heap-replay"
    installed = apps / "examples/heap_replay"
    shutil.copytree(fixture, installed, dirs_exist_ok=True)
    clock = repo / "tests/nuttx-std/memory-timing/qemu_timer_clock.c"
    shutil.copy2(clock, installed / clock.name)
    settings = {name: "y" for name in ("DEBUG_SYMBOLS", "DEBUG_LINK_MAP", "DEBUG_NOOPT", "LTO_NONE",
                "EXAMPLES_HEAP_REPLAY", "MM_DEFAULT_MANAGER", "BUILD_FLAT")}
    settings.update({name: "n" for name in ("SMP", "EXAMPLES_MEMORY_TIMING", "EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK",
        "MM_HEAP_MEMPOOL", "MM_KERNEL_HEAP", "MM_TLSF_MANAGER", "ARCH_FPU", "HAVE_CXX", "HAVE_CXXINITIALIZE",
        "NET", "TESTING_OSTEST", "TESTING_GETPRIME", "EXAMPLES_HELLO", "ARCH_PERF_EVENTS", "PERF_OVERFLOW_CORRECTION",
        "SCHED_INSTRUMENTATION", "DRIVERS_NOTE", "SYSTEM_TRACE", "MM_KASAN", "MM_FILL_ALLOCATIONS")})
    settings.update({"MM_REGIONS": "1", "MM_FREE_DELAYCOUNT_MAX": "0", "MM_BACKTRACE": "8",
                     "TASK_NAME_SIZE": "31", "MM_DEFAULT_ALIGNMENT": "8", "MM_NODE_GUARDSIZE": "0"})
    commands = []

    def execute(command, log):
        commands.append(command)
        subprocess.run(command, cwd=nuttx, stdout=log, stderr=subprocess.STDOUT, check=True)

    build_sources = {name: sha256(repo / name) for name in (
        "tools/memory-explorer/tests/build_timing_nuttx.py",
        "tools/memory-explorer/tests/build_replay_nuttx.py")}
    with atomic_stack_transition(nuttx, repo, output, commands) as atomic_stack:
        with (output / "configure.log").open("w") as log:
            config = nuttx / ".config"
            if not config.exists():
                execute(["bash", "tools/configure.sh", "-l", "-a", os.path.relpath(apps, nuttx), "mps2-an521:nsh"], log)
            elif "CONFIG_ARCH_BOARD_MPS2_AN521=y" not in config.read_text().splitlines():
                raise RuntimeError("The supplied build tree is configured for another board")
            # An already configured disposable tree may retain an examples index
            # generated before this fixture was installed. Use the native preconfig
            # target to regenerate it; do not edit upstream Kconfig source files.
            execute(["make", "-C", str(apps / "examples"), "APPDIR=" + str(apps),
                     "TOPDIR=" + str(nuttx), "preconfig"], log)
            lines = [line for line in config.read_text().splitlines() if not any(
                line.startswith("CONFIG_" + key + "=") or line == "# CONFIG_" + key + " is not set" for key in settings)]
            lines.extend("# CONFIG_" + key + " is not set" if value == "n" else "CONFIG_" + key + "=" + value
                         for key, value in settings.items())
            config.write_text("\n".join(lines) + "\n")
            execute(["make", "olddefconfig"], log)
            checks = validate_configuration(config.read_text(), settings)
            shutil.copy2(config, output / "resolved.config")
        with (output / "build.log").open("w") as log:
            execute(["make", "-j4", "EXTRALINKCMDS=" + " ".join("--wrap=" + name for name in WRAPPERS)], log)
        shutil.copy2(nuttx / "nuttx", output / "nuttx")
        shutil.copy2(nuttx / "nuttx.map", output / "nuttx.map")
        disassembly = subprocess.check_output(["arm-none-eabi-objdump", "-d", str(output / "nuttx")], text=True)
        (output / "disassembly.txt").write_text(disassembly)
        coverage = hook_coverage(disassembly)
        symbols = subprocess.check_output(["arm-none-eabi-nm", "-S", "--size-sort", str(output / "nuttx")], text=True)
        (output / "symbols.txt").write_text(symbols)
        fixture_functions = {"heap_replay_main", "replay_probe_stop", "probe", "safe_point", "arguments",
                             "__wrap_mm_lock", "__wrap_mm_unlock", "timing_clock_initialize", "timing_clock_stop",
                             "__wrap_up_perf_gettime", "__wrap_up_perf_getfreq", "__wrap_up_perf_convert"}
        linked_symbols = []
        for line in symbols.splitlines():
            parts = line.split()
            if len(parts) == 4 and (parts[3].startswith("g_replay_") or parts[3] == "g_clock_ready" or parts[3] in fixture_functions):
                linked_symbols.append({"name": parts[3], "address": int(parts[0], 16),
                                       "size": int(parts[1], 16), "kind": parts[2]})
    if build_sources != {name: sha256(repo / name) for name in build_sources}:
        raise RuntimeError("Native build helpers changed during the build")
    report = {"schema_version": 1, "workload": "native isolated heap replay qualification",
        "nuttx_revision": NUTTX_REVISION, "apps_revision": APPS_REVISION,
        "source_identity": identities, "configuration_checks": checks, "atomic_stack_patch": atomic_stack,
        "build_sources_sha256": build_sources, "commands": commands,
        "compiler": subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0],
        "fixture_sha256": {path.name: sha256(path) for path in sorted(fixture.iterdir()) if path.is_file()},
        "shared_clock_sha256": sha256(clock),
        "allocator_source_sha256": {name: sha256(nuttx / "mm/mm_heap" / name) for name in ALLOCATOR_SOURCES},
        "elf_sha256": sha256(output / "nuttx"), "config_sha256": sha256(output / "resolved.config"),
        "disassembly_sha256": sha256(output / "disassembly.txt"),
        "linked_fixture_symbols": linked_symbols,
        "linked_fixture_code_bytes": sum(item["size"] for item in linked_symbols if item["kind"].lower() == "t"),
        "linked_fixture_storage_bytes": sum(item["size"] for item in linked_symbols if item["kind"].lower() in ("b", "d")),
        "task_stack_reservation_bytes": 8192,
        "hook_coverage": coverage, "static_arena_bytes": 65536, "transaction_bound": 256,
        "clock_contract": {"clock": "qemu-an521-cmsdk-timer1-20mhz", "frequency_hz": 20000000,
                           "bits": 32, "epoch": "fixture invocation", "irq_enabled": False}}
    (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"build": str(output / "build.json"), "elf_sha256": report["elf_sha256"], "hook_coverage": coverage}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("nuttx", "apps", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(parser.parse_args())
