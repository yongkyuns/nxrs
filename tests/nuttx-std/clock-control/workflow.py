#!/usr/bin/env python3
"""Build and characterize the pinned ARM clock backend; defects are recorded."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TIMER = ROOT / "tests/nuttx-std/memory-timing/qemu_timer_clock.c"
NATIVE_ENV = ("NXRS_TARGET_C_FLAGS EXTRAFLAGS MAKEFLAGS MFLAGS GNUMAKEFLAGS MAKEOVERRIDES "
              "CC CXX CPP AS AR LD NM OBJCOPY STRIP CFLAGS CXXFLAGS CPPFLAGS AFLAGS LDFLAGS "
              "HOSTCC HOSTCXX HOSTCFLAGS HOSTCXXFLAGS HOSTLDFLAGS CPATH C_INCLUDE_PATH "
              "CPLUS_INCLUDE_PATH LIBRARY_PATH GCC_EXEC_PREFIX COMPILER_PATH SOURCE_DATE_EPOCH").split()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sources(qemu_clock_correction=False):
    paths = [HERE / name for name in ("Cargo.toml", "Cargo.lock", "native.c")]
    paths += sorted((HERE / "src").rglob("*.rs")) + [TIMER]
    if qemu_clock_correction:
        paths.append(HERE / "qemu_systick_clock.c")
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def identities():
    result = {"nxrs": git("rev-parse", "HEAD"), "working_tree": git("status", "--short")}
    for name in ("nuttx", "nuttx-apps"):
        expected = git("rev-parse", f"HEAD:external/{name}")
        actual = git("-C", f"external/{name}", "rev-parse", "HEAD")
        if actual != expected:
            raise ValueError(f"unpinned {name}: {actual} != {expected}")
        result[name] = actual
    return result


def validate_firmware(out, mode, tick_us):
    firmware = out / "firmware"
    config = firmware / "nuttx/.config"
    lines = set(config.read_text().splitlines())
    required = {"CONFIG_ARCH_BOARD_MPS2_AN521=y", "CONFIG_TIMER_ARCH=y",
                f"CONFIG_USEC_PER_TICK={tick_us}",
                "CONFIG_CLOCK_TIMEKEEPING=y" if mode == "software"
                else "# CONFIG_CLOCK_TIMEKEEPING is not set"}
    if not required <= lines:
        raise ValueError(f"effective configuration mismatch: {sorted(required - lines)}")
    if any(line.startswith("CONFIG_CLOCK_ADJTIME=") and line != "CONFIG_CLOCK_ADJTIME=n"
           for line in lines):
        raise ValueError("CLOCK_ADJTIME must be disabled")
    abi = json.loads((firmware / "abi-report.json").read_text())
    if (abi.get("probe_abi_compatible") is not True or abi.get("errors") != []
            or abi.get("target") != "thumbv8m.main-nuttx-eabi"
            or abi.get("config_sha256") != digest(config) or not abi.get("checks")):
        raise ValueError("missing, incompatible, or stale target ABI evidence")
    paths = [config, firmware / "abi-report.json", firmware / "provenance.txt",
             firmware / "resolved.config", firmware / "nuttx/nuttx"]
    return {str(path.relative_to(out)): digest(path) for path in paths}


def build(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = sources(args.qemu_clock_correction)
    command = ["cargo", "nxrs", "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--profile", "mps2-an521-mock", "--out", str(out / "firmware"),
               "--target-c-source", str(HERE / "native.c"), "--target-c-source", str(TIMER),
               "--kconfig-enable" if args.mode == "software" else "--kconfig-disable",
               "CLOCK_TIMEKEEPING", "--kconfig-disable", "CLOCK_ADJTIME",
               "--kconfig-set", f"USEC_PER_TICK={args.tick_us}"]
    environment = os.environ.copy()
    if args.qemu_clock_correction:
        command += ["--target-c-source", str(HERE / "qemu_systick_clock.c")]
        environment["MAKEFLAGS"] = (environment.get("MAKEFLAGS", "")
                                    + " EXTRALINKCMDS=--wrap=systick_initialize").strip()
    record = {"schema": 1, "mode": args.mode, "tick_us": args.tick_us,
              "qemu_clock_correction": args.qemu_clock_correction,
              "native_environment_sha256": {
                  name: hashlib.sha256(os.fsencode(environment[name])).hexdigest()
                  for name in NATIVE_ENV if name in environment},
              "command": command, "identities": identities(), "sources_before": before,
              "build_complete": False, "image": "firmware/nuttx/nuttx"}
    save(out / "build.json", record)
    try:
        with (out / "build.log").open("w") as log:
            subprocess.run(command, cwd=ROOT, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        record["sources_after"] = sources(args.qemu_clock_correction)
        if record["sources_after"] != before:
            raise ValueError("fixture sources changed during build")
        record["identities_after"] = identities()
        if any(record["identities_after"][name] != record["identities"][name]
               for name in ("nxrs", "nuttx", "nuttx-apps")):
            raise ValueError("repository identity changed during build")
        record["artifacts"] = validate_firmware(out, args.mode, args.tick_us)
        record["build_complete"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        record.setdefault("sources_after", sources(args.qemu_clock_correction))
        save(out / "build.json", record)
    print(f"Built {args.mode} clock characterization: {out}")


def run(args):
    out = args.build.resolve()
    record = json.loads((out / "build.json").read_text())
    if record.get("schema") != 1 or record.get("build_complete") is not True:
        raise ValueError("build is incomplete")
    correction = record.get("qemu_clock_correction", False)
    if sources(correction) != record["sources_after"]:
        raise ValueError("fixture sources differ from the built inputs")
    artifacts = validate_firmware(out, record["mode"], record["tick_us"])
    if artifacts != record["artifacts"]:
        raise ValueError("firmware or retained build evidence changed")
    executable = shutil.which("qemu-system-arm")
    if executable is None:
        raise ValueError("qemu-system-arm is required on PATH")
    captures = out / "captures"
    captures.mkdir(exist_ok=False)
    summary = {"mode": record["mode"], "tick_us": record["tick_us"], "runs": [],
               "qemu_clock_correction": correction,
               "build_sha256": digest(out / "build.json"), "artifacts": artifacts,
               "qemu_version": subprocess.check_output([executable, "--version"], text=True),
               "qemu_sha256": digest(Path(executable)), "characterization_complete": False,
               "qualified": False, "physical_rtc_retention_qualified": False}
    oracle = load_module("clock_oracle", HERE / "oracle.py")
    harness = load_module("clock_nuttx_runner", HERE.parent / "run.py")
    summary["oracle_sha256"] = digest(HERE / "oracle.py")

    def verify(text, status, mode):
        values = re.findall(r"^NXRS_STATUS_([0-9]+)\s*$", status.replace("\r", ""), re.M)
        if values != ["0"]:
            raise ValueError(f"clock probe did not return success: {values}")
        result = oracle.analyze(text, mode, record["tick_us"])
        if type(result.get("qualified")) is not bool:
            raise ValueError("oracle omitted its qualification verdict")
        summary["runs"].append(result)

    try:
        cases = [(record["mode"], "clock_probe") for _ in range(args.repeat)]
        harness.run_cases(out / record["image"], captures, cases, verify, arm_mps2=True)
        if validate_firmware(out, record["mode"], record["tick_us"]) != artifacts:
            raise ValueError("firmware or build evidence changed during capture")
        summary["characterization_complete"] = True
        summary["qualified"] = all(result["qualified"] for result in summary["runs"])
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        save(out / "analysis.json", summary)
    print(f"Characterization complete; backend qualified={summary['qualified']}: {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compile_ = commands.add_parser("build")
    compile_.add_argument("--mode", choices=("baseline", "software"), required=True)
    compile_.add_argument("--out", type=Path, required=True)
    compile_.add_argument("--tick-us", type=int, default=1000)
    compile_.add_argument("--qemu-clock-correction", action="store_true",
                          help="explicit fixture correction for QEMU's 20 MHz SysTick clock")
    execute = commands.add_parser("run")
    execute.add_argument("--build", type=Path, required=True)
    execute.add_argument("--repeat", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    if args.command == "build" and args.tick_us <= 0:
        parser.error("--tick-us must be positive")
    (build if args.command == "build" else run)(args)


if __name__ == "__main__":
    main()
