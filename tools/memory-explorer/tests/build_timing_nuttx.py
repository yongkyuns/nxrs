#!/usr/bin/env python3
"""Build the native timing witness in supplied disposable pinned NuttX trees.

This deliberately does not claim to build an NXRS application or product profile.
It installs one test application and resolves its Kconfig through normal NuttX
make targets, retaining the effective configuration, source identities and flags.
Put the ARM cross tools and kconfig-conf on PATH before invocation.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


NUTTX_REVISION = "2b5c0de5e933281c209f954a63b99cf10c132346"
APPS_REVISION = "3c772ea35b9245972e18999828f7b533e6fc57cc"
ATOMIC_STACK_PATCH = "upstream/nuttx/patches/0150-armv8m-atomic-stack-transition.patch"
ATOMIC_STACK_SOURCE = "arch/arm/src/armv8-m/arm_initialstate.c"
ATOMIC_STACK_BEFORE = "bad2478f501954bdf252ca71280b4025c5ed29ade912b4d94fc8f0957946520f"
ATOMIC_STACK_AFTER = "f1a091dd7bf23825cdb5e9563cc37718a2c90b38014049919d0e7eb2d9528666"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def atomic_stack_transition(nuttx, repository, output, commands):
    """Temporarily apply the exact pinned ARM startup fix for a native build."""
    patch = repository / ATOMIC_STACK_PATCH
    retained = output / "atomic-stack.patch"
    shutil.copy2(patch, retained)
    patch_bytes = retained.read_bytes()
    paths = re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch_bytes.decode(), re.MULTILINE)
    if paths != [(ATOMIC_STACK_SOURCE, ATOMIC_STACK_SOURCE)]:
        raise RuntimeError("Atomic stack patch changes an unexpected source")
    source = nuttx / ATOMIC_STACK_SOURCE
    if sha256(source) != ATOMIC_STACK_BEFORE:
        raise RuntimeError("Atomic stack source differs from its pinned preimage")
    record = {"path": ATOMIC_STACK_PATCH, "sha256": sha256(retained),
              "source": ATOMIC_STACK_SOURCE, "before": ATOMIC_STACK_BEFORE,
              "after": ATOMIC_STACK_AFTER, "source_restored": False}
    with (output / "atomic-stack-patch.log").open("w") as log:
        def apply(*flags):
            command = ["git", "-c", "core.autocrlf=false", "apply", *flags, "-"]
            commands.append(command)
            subprocess.run(command, input=patch_bytes, cwd=nuttx, stdout=log,
                           stderr=subprocess.STDOUT, check=True)

        apply("--check")
        apply()
        try:
            if sha256(source) != ATOMIC_STACK_AFTER:
                raise RuntimeError("Atomic stack patch produced an unexpected source")
            yield record
            if sha256(source) != ATOMIC_STACK_AFTER:
                raise RuntimeError("Atomic stack source changed during the native build")
            if sha256(patch) != record["sha256"] or sha256(retained) != record["sha256"]:
                raise RuntimeError("Atomic stack patch changed during the native build")
        finally:
            # Reverse the bytes actually applied, even if the repository patch
            # changes or the compiler fails while this context is active.
            apply("--reverse", "--check")
            apply("--reverse")
            if sha256(source) != ATOMIC_STACK_BEFORE:
                raise RuntimeError("Atomic stack source was not restored exactly")
            record["source_restored"] = True


def validate_atomic_stack_patch(build, repository, build_dir=None):
    expected = {"path": ATOMIC_STACK_PATCH, "sha256": sha256(repository / ATOMIC_STACK_PATCH),
                "source": ATOMIC_STACK_SOURCE, "before": ATOMIC_STACK_BEFORE,
                "after": ATOMIC_STACK_AFTER, "source_restored": True}
    if build.get("atomic_stack_patch") != expected:
        raise RuntimeError("Native build atomic stack patch/source identity differs")
    if build_dir is not None and sha256(build_dir / "atomic-stack.patch") != expected["sha256"]:
        raise RuntimeError("Retained atomic stack patch differs from the build receipt")


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
    # A tilde inside a component is literal, including Windows short names.
    if not value or value.startswith("~") or any(character.isspace() or character in "$`\\\"';|&<>(){}[]*?!#"
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
    identities = {"nuttx": source_identity(nuttx, NUTTX_REVISION), "apps": source_identity(apps, APPS_REVISION)}
    output.mkdir(parents=True, exist_ok=False)
    repository = Path(__file__).resolve().parents[3]
    fixture = repository / "tests" / "nuttx-std" / "memory-timing"
    installed_fixture = apps / "examples" / "memory_timing"
    shutil.copytree(fixture, installed_fixture, dirs_exist_ok=True)
    # Exercise the exact production ABI translator, not a fixture substitute.
    bridge = repository / "shared/acquisition/src/platform/nuttx/ffi/trace.c"
    shutil.copy2(bridge, installed_fixture / "acquisition_trace.c")
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

    build_sources = {name: sha256(repository / name) for name in ('tools/memory-explorer/tests/build_timing_nuttx.py',)}
    with atomic_stack_transition(nuttx, repository, output, commands) as atomic_stack:
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
    if build_sources != {name: sha256(repository / name) for name in build_sources}:
        raise RuntimeError("Native build helpers changed during the build")
    report = {"schema_version": 1, "workload": "native NuttX memory_timing qualification fixture",
              "nuttx_revision": NUTTX_REVISION, "apps_revision": APPS_REVISION,
              "source_identity": identities, "configuration_checks": checks, "atomic_stack_patch": atomic_stack,
              "build_sources_sha256": build_sources,
              "qemu_cmsdk_clock_adapter": args.qemu_timer_clock, "commands": commands,
              "clock_contract": {"resource": "otherwise unused AN521 CMSDK APB timer1",
                                 "secure_address": 0x50001000, "frequency_hz": 20000000,
                                 "bits": 32, "wrap_period_ns": 214748364800,
                                 "epoch": "reset at fixture invocation", "irq_enabled": False,
                                 "scope": "explicit QEMU qualification only"} if args.qemu_timer_clock else None,
              "compiler": subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0],
              "acquisition_trace_bridge": {
                  "source": bridge.relative_to(repository).as_posix(),
                  "sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
                  "installed_sha256": hashlib.sha256((installed_fixture / "acquisition_trace.c").read_bytes()).hexdigest(),
              },
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
