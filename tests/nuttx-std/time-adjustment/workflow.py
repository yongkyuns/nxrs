#!/usr/bin/env python3
"""Build and qualify public software adjustment using explicit QEMU virtual time."""
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
PHASE1 = HERE.parent / "clock-control"
spec = importlib.util.spec_from_file_location("clock_build_support", PHASE1 / "workflow.py")
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)
digest, save = support.digest, support.save
HELPERS = [PHASE1 / "native.c", PHASE1 / "qemu_systick_clock.c",
           HERE.parent / "memory-timing/qemu_timer_clock.c"]
PATCH = ROOT / "upstream/nuttx/patches/0150-clock-timekeeping-adjustment.patch"
ICOUNT = ["-icount", "shift=3,sleep=off"]


def sources():
    paths = [HERE / "Cargo.toml", HERE / "Cargo.lock", PATCH,
             ROOT / "upstream/build-patches.json", *HELPERS]
    paths += sorted((HERE / "src").rglob("*.rs"))
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def validate(out, tick_us):
    firmware = out / "firmware"
    config = firmware / "nuttx/.config"
    lines = set(config.read_text().splitlines())
    required = {"CONFIG_ARCH_BOARD_MPS2_AN521=y", "CONFIG_TIMER_ARCH=y",
                "CONFIG_CLOCK_TIMEKEEPING=y", "CONFIG_CLOCK_ADJTIME=y",
                f"CONFIG_USEC_PER_TICK={tick_us}"}
    if not required <= lines or {"CONFIG_RTC=y", "CONFIG_RTC_HIRES=y"} & lines:
        raise ValueError("effective public software-clock configuration mismatch")
    abi = json.loads((firmware / "abi-report.json").read_text())
    if (abi.get("probe_abi_compatible") is not True or abi.get("errors") != []
            or abi.get("target") != "thumbv8m.main-nuttx-eabi"
            or abi.get("config_sha256") != digest(config) or not abi.get("checks")):
        raise ValueError("missing, incompatible, or stale ABI evidence")
    ledger = json.loads((firmware / "nuttx-patches.json").read_text())
    matches = [entry for entry in ledger["patches"] if entry["name"] == PATCH.name]
    if len(matches) != 1 or matches[0]["sha256"] != digest(PATCH):
        raise ValueError("software adjustment patch is absent or differs from its receipt")
    names = ["nuttx/.config", "nuttx/nuttx", "resolved.config", "abi-report.json",
             "provenance.txt", "nuttx-patches.json", "nuttx-apps-patches.json", "std-patch.json"]
    return {f"firmware/{name}": digest(firmware / name) for name in names}


def build(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = sources()
    command = ["cargo", "nxrs", "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--profile", "mps2-an521-mock", "--out", str(out / "firmware"),
               "--kconfig-enable", "CLOCK_TIMEKEEPING", "--kconfig-enable", "CLOCK_ADJTIME",
               "--kconfig-set", f"USEC_PER_TICK={args.tick_us}"]
    for helper in HELPERS:
        command += ["--target-c-source", str(helper)]
    environment = os.environ.copy()
    override = "EXTRALINKCMDS=--wrap=systick_initialize"
    environment["MAKEFLAGS"] = (environment.get("MAKEFLAGS", "") + " " + override).strip()
    record = {"schema": 1, "tick_us": args.tick_us, "command": command,
              "identities": support.identities(), "sources_before": before,
              "qemu_clock_correction": True, "native_linker_override": override,
              "native_environment_sha256": {
                  name: hashlib.sha256(os.fsencode(environment[name])).hexdigest()
                  for name in support.NATIVE_ENV if name in environment},
              "workflow_sha256": digest(Path(__file__)), "build_complete": False,
              "image": "firmware/nuttx/nuttx"}
    save(out / "build.json", record)
    try:
        with (out / "build.log").open("w") as log:
            subprocess.run(command, cwd=ROOT, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        record["sources_after"] = sources()
        if record["sources_after"] != before:
            raise ValueError("compiled inputs changed during build")
        record["identities_after"] = support.identities()
        if any(record["identities_after"][key] != record["identities"][key]
               for key in ("nxrs", "nuttx", "nuttx-apps")):
            raise ValueError("repository identity changed during build")
        record["artifacts"] = validate(out, args.tick_us)
        record["build_complete"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        record.setdefault("sources_after", sources())
        save(out / "build.json", record)
    print(f"Built public adjustment contract: {out}")


def run(args):
    out = args.build.resolve()
    build_record = json.loads((out / "build.json").read_text())
    if build_record.get("schema") != 1 or build_record.get("build_complete") is not True:
        raise ValueError("incomplete build")
    if sources() != build_record["sources_after"]:
        raise ValueError("compiled inputs changed after build")
    artifacts = validate(out, build_record["tick_us"])
    if artifacts != build_record["artifacts"]:
        raise ValueError("firmware or retained evidence changed")
    executable = shutil.which("qemu-system-arm")
    if executable is None:
        raise ValueError("qemu-system-arm is required on PATH")
    captures = out / "captures"
    captures.mkdir(exist_ok=False)
    oracle = support.load_module("public_adjustment_oracle", HERE / "oracle.py")
    harness_path = HERE.parent / "run.py"
    harness = support.load_module("public_adjustment_runner", harness_path)
    original_command = harness.qemu_command
    harness.qemu_command = lambda *a, **k: original_command(*a, **k) + ICOUNT
    summary = {"schema": 1, "tick_us": build_record["tick_us"], "runs": [],
               "build_sha256": digest(out / "build.json"), "artifacts": artifacts,
               "oracle_sha256": digest(HERE / "oracle.py"),
               "workflow_sha256": digest(Path(__file__)), "harness_sha256": digest(harness_path),
               "qemu_version": subprocess.check_output([executable, "--version"], text=True),
               "qemu_sha256": digest(Path(executable)), "qemu_clock_options": ICOUNT,
               "virtual_ns_per_instruction": 8, "cycle_accurate": False,
               "physical_rtc_retention_qualified": False,
               "characterization_complete": False, "qualified": False}

    def verify(text, status, _mode):
        if re.findall(r"^NXRS_STATUS_([0-9]+)\s*$", status.replace("\r", ""), re.M) != ["0"]:
            raise ValueError("public adjustment probe did not return success")
        summary["runs"].append(oracle.analyze(text, build_record["tick_us"]))

    try:
        harness.run_cases(out / build_record["image"], captures,
                          [("public", "time_adjustment")] * args.repeat, verify, arm_mps2=True)
        if sources() != build_record["sources_after"] or validate(out, build_record["tick_us"]) != artifacts:
            raise ValueError("compiled inputs or build evidence changed during capture")
        if digest(HERE / "oracle.py") != summary["oracle_sha256"]:
            raise ValueError("oracle changed during capture")
        summary["characterization_complete"] = True
        summary["qualified"] = all(result["qualified"] for result in summary["runs"])
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        save(out / "analysis.json", summary)
    print(f"Public adjustment qualified={summary['qualified']}: {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compile_ = commands.add_parser("build")
    compile_.add_argument("--out", type=Path, required=True)
    compile_.add_argument("--tick-us", type=int, default=100)
    execute = commands.add_parser("run")
    execute.add_argument("--build", type=Path, required=True)
    execute.add_argument("--repeat", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    if args.command == "build" and args.tick_us <= 0:
        parser.error("--tick-us must be positive")
    (build if args.command == "build" else run)(args)


if __name__ == "__main__":
    main()
