#!/usr/bin/env python3
"""Build the finite capture witness with the exact production patches.

Supply disposable, tracked-clean pinned native trees. The production patch is
applied only for this build and reversed afterwards, including build failures.
Existing native timing/lifecycle fixtures and their clock contracts are unchanged.
"""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from build_timing_nuttx import (
    APPS_REVISION, NUTTX_REVISION, ATOMIC_STACK_SOURCE, atomic_stack_transition,
    source_identity, validate_apps_directory, validate_configuration,
)


PRODUCTION_PATCH = "upstream/nuttx-apps/patches/0007-trace-capture-window.patch"
SYSTICK_PATCH = "upstream/nuttx/patches/0149-armv8m-systick-coherent-status.patch"
SYSTICK_SOURCE = "arch/arm/src/armv8-m/arm_systick.c"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_files(text):
    paths = re.findall(r"^\+\+\+ b/(.+)$", text, re.MULTILINE)
    if not paths or len(paths) != len(set(paths)) or any(
        not path.startswith("system/trace/") or ".." in Path(path).parts
        for path in paths
    ):
        raise RuntimeError("The production patch must modify only unique system/trace files")
    return paths


def run(args):
    nuttx, apps, output = [getattr(args, name).resolve() for name in ("nuttx", "apps", "output")]
    config = nuttx / ".config"
    if config.exists():
        validate_apps_directory(nuttx, apps, config.read_text())
        if "CONFIG_ARCH_BOARD_MPS2_AN521=y" not in config.read_text().splitlines():
            raise RuntimeError("The disposable build tree is configured for another board")
    identities = {"nuttx": source_identity(nuttx, NUTTX_REVISION),
                  "apps": source_identity(apps, APPS_REVISION)}
    repository = Path(__file__).resolve().parents[3]
    build_sources = {name: sha256(repository / name) for name in (
        "tools/memory-explorer/tests/build_timed_capture_nuttx.py",
        "tools/memory-explorer/tests/build_timing_nuttx.py")}
    patch = repository / PRODUCTION_PATCH
    paths = patch_files(patch.read_text())
    original = {path: sha256(apps / path) if (apps / path).is_file() else None for path in paths}
    tracked = set(subprocess.check_output(
        ["git", "-C", str(apps), "ls-files", "--", *paths], text=True).splitlines())
    if any(original[path] is not None and path not in tracked for path in paths):
        raise RuntimeError("An untracked file collides with a production patch input")
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(patch, output / "production.patch")
    systick = output / "systick.patch"
    shutil.copy2(repository / SYSTICK_PATCH, systick)
    original_systick = sha256(nuttx / SYSTICK_SOURCE)
    # Applying and reversing the retained bytes remains safe if another task
    # updates the repository patch while this build is running. Such a change
    # still rejects the success receipt below.
    patch = output / "production.patch"
    fixture = repository / "tests/nuttx-std/timed-capture"
    installed = apps / "examples/timed_capture"
    shutil.copytree(fixture, installed, dirs_exist_ok=True)
    fixture_hashes = {path.name: sha256(path) for path in sorted(fixture.iterdir()) if path.is_file()}
    installed_hashes = {name: sha256(installed / name) for name in fixture_hashes}
    if installed_hashes != fixture_hashes:
        raise RuntimeError("Installed fixture differs from its source at copy time")
    copied = {
        "acquisition_trace.c": repository / "shared/acquisition/src/platform/nuttx/ffi/trace.c",
        "qemu_timer_clock.c": repository / "tests/nuttx-std/memory-timing/qemu_timer_clock.c",
    }
    for name, source in copied.items():
        shutil.copy2(source, installed / name)
    copied_hashes = {name: {"source": source.relative_to(repository).as_posix(),
                            "sha256": sha256(source), "installed_sha256": sha256(installed / name)}
                     for name, source in copied.items()}
    if any(item["sha256"] != item["installed_sha256"] for item in copied_hashes.values()):
        raise RuntimeError("Installed native bridge/clock differs at copy time")
    settings = {name: "y" for name in (
        "DEBUG_SYMBOLS", "DEBUG_LINK_MAP", "SCHED_INSTRUMENTATION", "SCHED_INSTRUMENTATION_FILTER",
        "SCHED_INSTRUMENTATION_SWITCH", "SCHED_INSTRUMENTATION_IRQHANDLER", "SCHED_INSTRUMENTATION_DUMP",
        "DRIVERS_NOTE", "DRIVERS_NOTECTL", "DRIVERS_NOTERAM", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE",
        "EXAMPLES_TIMED_CAPTURE", "SYSTEM_TRACE", "SYSTEM_TRACE_CAPTURE", "SCHED_WAITPID", "PTHREAD_MUTEX_BOTH",
        "SIG_DEFAULT", "SIG_SIGKILL_ACTION",
    )}
    settings.update({name: "n" for name in (
        "SMP", "ARCH_FPU", "HAVE_CXX", "HAVE_CXXINITIALIZE", "NET", "DEBUG_SCHED", "DEBUG_SCHED_ERROR",
        "TESTING_OSTEST", "TESTING_GETPRIME", "EXAMPLES_HELLO", "ARCH_PERF_EVENTS", "PERF_OVERFLOW_CORRECTION",
        "EXAMPLES_MEMORY_TIMING", "EXAMPLES_MEMORY_TIMING_CMSDK_CLOCK", "SCHED_INSTRUMENTATION_WDOG",
        "SCHED_INSTRUMENTATION_HEAP", "SCHED_INSTRUMENTATION_SYSCALL", "BUILD_PROTECTED", "BUILD_KERNEL",
        "EXAMPLES_HEAP_REPLAY", "EXAMPLES_HEAP_REQUESTS", "PTHREAD_MUTEX_UNSAFE",
    )})
    settings.update({"DRIVERS_NOTERAM_BUFSIZE": "32768", "DRIVERS_NOTE_TASKNAME_BUFSIZE": "4096",
                     "SCHED_INSTRUMENTATION_FILTER_DEFAULT_MODE": "0x0", "SYSTEM_TRACE_STACKSIZE": "4096",
                     "TASK_NAME_SIZE": "31", "DEBUG_SYMBOLS_LEVEL": '"-g3"'})
    commands = []

    def execute(command, cwd, log):
        commands.append(command)
        subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)

    def reverse_patch(source, retained, log):
        execute(["git", "apply", "--reverse", "--check", str(retained)], source, log)
        execute(["git", "apply", "--reverse", str(retained)], source, log)

    with (output / "patch.log").open("w") as patch_log:
        with ExitStack() as restore:
            atomic_stack = restore.enter_context(atomic_stack_transition(nuttx, repository, output, commands))
            # The production trace command uses CLOCK_MONOTONIC for its watchdog.
            # Keep its existing clock checks on the corrected Cortex-M SysTick.
            for source, retained in ((nuttx, systick), (apps, patch)):
                execute(["git", "apply", "--check", str(retained)], source, patch_log)
                execute(["git", "apply", str(retained)], source, patch_log)
                restore.callback(reverse_patch, source, retained, patch_log)
            kernel_changes = subprocess.check_output(
                ["git", "-C", str(nuttx), "diff", "--name-only", "HEAD", "--"], text=True).splitlines()
            if sorted(kernel_changes) != sorted([SYSTICK_SOURCE, ATOMIC_STACK_SOURCE]):
                raise RuntimeError("Native kernel changes differ from the exact startup and SysTick patches")
            patched_systick = sha256(nuttx / SYSTICK_SOURCE)
            changed = set(subprocess.check_output(
                ["git", "-C", str(apps), "diff", "--name-only", "HEAD", "--"], text=True).splitlines())
            if changed != tracked:
                raise RuntimeError("Native tracked changes differ from the exact production patch")
            patched = {path: sha256(apps / path) for path in paths}
            with (output / "configure.log").open("w") as log:
                config = nuttx / ".config"
                if not config.exists():
                    execute(["bash", "tools/configure.sh", "-l", "-a", os.path.relpath(apps, nuttx),
                             "mps2-an521:nsh"], nuttx, log)
                elif "CONFIG_ARCH_BOARD_MPS2_AN521=y" not in config.read_text().splitlines():
                    raise RuntimeError("The disposable build tree is configured for another board")
                lines = [line for line in config.read_text().splitlines()
                         if not any(line.startswith("CONFIG_" + key + "=") or
                                    line == "# CONFIG_" + key + " is not set" for key in settings)]
                lines.extend("# CONFIG_" + key + " is not set" if value == "n" else "CONFIG_" + key + "=" + value
                             for key, value in settings.items())
                config.write_text("\n".join(lines) + "\n")
                # Apps' top-level generated Kconfig can already exist in a
                # reused tree. Regenerate its examples menu through the native
                # Make target after installing the new fixture directory.
                execute(["make", "-C", str(apps / "examples"), "preconfig",
                         "APPDIR=" + str(apps), "TOPDIR=" + str(nuttx)], nuttx, log)
                execute(["make", "olddefconfig"], nuttx, log)
                configured_apps = validate_apps_directory(nuttx, apps, config.read_text())
                checks = validate_configuration(config.read_text(), settings)
                shutil.copy2(config, output / "resolved.config")
            with (output / "build.log").open("w") as log:
                # Changing configured applications in a reused tree must not
                # retain old archive members or assembly dependency artifacts.
                execute(["make", "clean"], nuttx, log)
                execute(["make", "-j2", "EXTRALINKCMDS=--wrap=up_perf_gettime --wrap=up_perf_getfreq --wrap=up_perf_convert"],
                        nuttx, log)
            if (patched != {path: sha256(apps / path) for path in paths} or
                    patched_systick != sha256(nuttx / SYSTICK_SOURCE)):
                raise RuntimeError("Production source changed during the native build")
            if (fixture_hashes != {path.name: sha256(path) for path in sorted(fixture.iterdir()) if path.is_file()} or
                installed_hashes != {name: sha256(installed / name) for name in installed_hashes} or
                any(item["sha256"] != sha256(repository / item["source"]) or
                    item["installed_sha256"] != sha256(installed / name)
                    for name, item in copied_hashes.items())):
                raise RuntimeError("Fixture or copied native source changed during the build")
            shutil.copy2(nuttx / "nuttx", output / "nuttx")
            if (nuttx / "nuttx.map").is_file():
                shutil.copy2(nuttx / "nuttx.map", output / "nuttx.map")
            symbols = subprocess.check_output(["arm-none-eabi-nm", "-S", str(output / "nuttx")], text=True)
            for symbol in ("trace_main", "timed_capture_main", "__wrap_up_perf_gettime"):
                if not re.search(r"\b" + symbol + r"$", symbols, re.MULTILINE):
                    raise RuntimeError("Missing actual linked witness/production symbol: " + symbol)
            (output / "symbols.txt").write_text(symbols)
    restored = source_identity(apps, APPS_REVISION)
    restored_nuttx = source_identity(nuttx, NUTTX_REVISION)
    if (sha256(repository / PRODUCTION_PATCH) != sha256(patch) or
            sha256(repository / SYSTICK_PATCH) != sha256(systick)):
        raise RuntimeError("The repository production patch changed during the native build")
    if build_sources != {name: sha256(repository / name) for name in build_sources}:
        raise RuntimeError("Native build helpers changed during the build")
    report = {
        "schema_version": 1,
        "workload": "production trace capture builtin with simulated slow operations",
        "nuttx_revision": NUTTX_REVISION,
        "apps_revision": APPS_REVISION,
        "source_identity_before_patch": identities,
        "apps_tracked_sources_restored": restored["tracked_sources_clean"],
        "nuttx_tracked_sources_restored": restored_nuttx["tracked_sources_clean"],
        "atomic_stack_patch": atomic_stack,
        "systick_patch": {"path": SYSTICK_PATCH, "sha256": sha256(systick),
                          "source": SYSTICK_SOURCE, "before": original_systick,
                          "after": patched_systick},
        "production_patch": {"path": PRODUCTION_PATCH, "sha256": sha256(patch),
                             "original_sources": original, "patched_sources": patched},
        "fixture_sha256": fixture_hashes,
        "installed_fixture_sha256": installed_hashes,
        "copied_sources": copied_hashes,
        "build_sources_sha256": build_sources,
        "configuration_checks": checks,
        "configured_apps_directory": str(configured_apps),
        "commands": commands,
        "compiler": subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0],
        "clock_contract": {"resource": "otherwise unused QEMU AN521 CMSDK APB timer1",
                           "frequency_hz": 20000000, "bits": 32, "wrap_period_ns": 214748364800,
                           "epoch": "fixture invocation; production command does not reset it",
                           "sys_tick_sleep_duration_is_not_an_oracle": True},
        "elf_sha256": sha256(output / "nuttx"),
        "config_sha256": sha256(output / "resolved.config"),
    }
    (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"build": str(output / "build.json"), "elf_sha256": report["elf_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("nuttx", "apps", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(parser.parse_args())
