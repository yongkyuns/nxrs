#!/usr/bin/env python3
"""Qualify the production Rust time APIs in an isolated, explicit QEMU image."""
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
SUPPORT = HERE.parent / "clock-control/workflow.py"
spec = importlib.util.spec_from_file_location("time_sync_build_support", SUPPORT)
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)
digest, save = support.digest, support.save
PROFILE = "mps2-an521-time-sync"
PATCH = ROOT / "upstream/nuttx/patches/0150-clock-timekeeping-adjustment.patch"
HELPERS = [HERE / "reference.c", HERE.parent / "clock-control/qemu_systick_clock.c",
           HERE.parent / "memory-timing/qemu_timer_clock.c"]
ICOUNT = ["-icount", "shift=3,sleep=off"]


def sources():
    paths = [HERE / "Cargo.toml", HERE / "Cargo.lock", ROOT / "Cargo.toml",
             ROOT / "Cargo.lock", PATCH, ROOT / "upstream/build-patches.json",
             ROOT / f"tests/profiles/{PROFILE}.toml",
             ROOT / "tests/profiles/mps2-an521-mock.toml", SUPPORT, *HELPERS]
    paths += sorted(HERE.glob("*.py")) + sorted((HERE / "src").rglob("*.rs"))
    for crate in (ROOT / "hal/time", ROOT / "service/time-sync"):
        paths += [crate / "Cargo.toml"]
        paths += sorted((crate / "src").rglob("*.rs"))
        paths += sorted((crate / "src").rglob("*.c"))
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted(set(paths))}


def validate(out, variant):
    firmware = out / "firmware"
    config = firmware / "nuttx/.config"
    lines = set(config.read_text().splitlines())
    required = {"CONFIG_ARCH_BOARD_MPS2_AN521=y", "CONFIG_TIMER_ARCH=y",
                "CONFIG_CLOCK_TIMEKEEPING=y", "CONFIG_CLOCK_ADJTIME=y",
                "CONFIG_USEC_PER_TICK=100"}
    if not required <= lines or {"CONFIG_RTC=y", "CONFIG_RTC_HIRES=y"} & lines:
        raise ValueError("effective public software-clock configuration mismatch")
    abi = json.loads((firmware / "abi-report.json").read_text())
    if (abi.get("probe_abi_compatible") is not True or abi.get("errors") != []
            or abi.get("target") != "thumbv8m.main-nuttx-eabi"
            or abi.get("config_sha256") != digest(config) or not abi.get("checks")):
        raise ValueError("missing, incompatible, or stale target ABI evidence")
    ledger = json.loads((firmware / "nuttx-patches.json").read_text())
    patches = [entry for entry in ledger["patches"] if entry["name"] == PATCH.name]
    if len(patches) != 1 or patches[0]["sha256"] != digest(PATCH):
        raise ValueError("software adjustment patch differs from the target receipt")
    selection = json.loads((firmware / "provider-selection.json").read_text())
    if selection.get("platform") != PROFILE or not any(
            p.get("capability") == "time" and p.get("implementation") == "nuttx"
            for p in selection.get("providers", [])):
        raise ValueError("explicit time=NuttX qualification profile is missing")
    graph = selection["dependency_resolution"]
    if graph.get("status") != "resolved":
        raise ValueError("the target dependency graph was not resolved")
    for package in ("nxrs-time", "nxrs-time-sync-service"):
        present = any(p.startswith(package + " v") for p in graph["packages"])
        if present != (variant == "controller"):
            raise ValueError(f"{package} does not match the selected software variant")
    names = ["nuttx/.config", "nuttx/nuttx", "nuttx/nuttx.bin", "resolved.config",
             "abi-report.json", "provenance.txt", "nuttx-patches.json",
             "nuttx-apps-patches.json", "std-patch.json", "std-source.json",
             "provider-selection.json", "resolved-profile.json", "native-context.json",
             "rust-link.json", "image-size.txt", "image-layout.txt", "symbols.txt"]
    return {f"firmware/{name}": digest(firmware / name) for name in names}


def compiler_identity(firmware):
    source = json.loads((firmware / "std-source.json").read_text())
    if source.get("selected_source_matches") is not True:
        raise ValueError("target standard library source is not verified")
    rustc = Path(source["rustc"])
    cc = shutil.which("arm-none-eabi-gcc")
    if cc is None:
        raise ValueError("target C compiler is missing")
    return {"rustc_sha256": digest(rustc),
            "rustc_version": subprocess.check_output([str(rustc), "-vV"], text=True),
            "cc_sha256": digest(Path(cc)),
            "cc_version": subprocess.check_output([cc, "--version"], text=True),
            "std_patch_sha256": digest(firmware / "std-patch.json")}


def build(args):
    out = args.out.resolve()
    before = sources()
    out.mkdir(parents=True, exist_ok=False)
    command = ["cargo", "nxrs", "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--profile", PROFILE, "--out", str(out / "firmware"),
               "--kconfig-enable", "CLOCK_TIMEKEEPING", "--kconfig-enable", "CLOCK_ADJTIME",
               "--kconfig-set", "USEC_PER_TICK=100"]
    if args.variant == "controller":
        command += ["--features", "controller"]
    for helper in HELPERS:
        command += ["--target-c-source", str(helper)]
    environment = os.environ.copy()
    override = "EXTRALINKCMDS=--wrap=systick_initialize"
    environment["MAKEFLAGS"] = (environment.get("MAKEFLAGS", "") + " " + override).strip()
    record = {"schema": 1, "variant": args.variant, "tick_us": 100, "command": command,
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
        record["artifacts"] = validate(out, args.variant)
        record["compiler"] = compiler_identity(out / "firmware")
        record["build_complete"] = True
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        record.setdefault("sources_after", sources())
        save(out / "build.json", record)
    print(f"Built {args.variant} production API contract: {out}")


def checked_build(out):
    record = json.loads((out / "build.json").read_text())
    if record.get("schema") != 1 or record.get("build_complete") is not True:
        raise ValueError("incomplete build")
    if sources() != record["sources_after"]:
        raise ValueError("compiled inputs changed after build")
    if validate(out, record["variant"]) != record["artifacts"]:
        raise ValueError("firmware or retained build evidence changed")
    return record


def run(args):
    out = args.build.resolve()
    record = checked_build(out)
    executable = shutil.which("qemu-system-arm")
    if executable is None:
        raise ValueError("qemu-system-arm is required on PATH")
    captures = out / "captures"
    captures.mkdir(exist_ok=False)
    oracle = support.load_module("time_sync_oracle", HERE / "oracle.py")
    harness_path = HERE.parent / "run.py"
    harness = support.load_module("time_sync_runner", harness_path)
    original_command = harness.qemu_command
    harness.qemu_command = lambda *a, **k: original_command(*a, **k) + ICOUNT
    summary = {"schema": 1, "variant": record["variant"], "runs": [],
               "build_sha256": digest(out / "build.json"), "artifacts": record["artifacts"],
               "oracle_sha256": digest(HERE / "oracle.py"),
               "workflow_sha256": digest(Path(__file__)), "harness_sha256": digest(harness_path),
               "qemu_version": subprocess.check_output([executable, "--version"], text=True),
               "qemu_sha256": digest(Path(executable)), "qemu_clock_options": ICOUNT,
               "virtual_ns_per_instruction": 8, "cycle_accurate": False,
               "physical_rtc_retention_qualified": False, "qualified": False}

    def verify(text, status, _mode):
        if re.findall(r"^NXRS_STATUS_([0-9]+)\s*$", status.replace("\r", ""), re.M) != ["0"]:
            raise ValueError("production time API fixture did not return success")
        summary["runs"].append(oracle.analyze(text, record["variant"]))

    try:
        harness.run_cases(out / record["image"], captures,
                          [(record["variant"], "time_sync")] * args.repeat,
                          verify, arm_mps2=True)
        checked_build(out)
        if digest(HERE / "oracle.py") != summary["oracle_sha256"]:
            raise ValueError("oracle changed during capture")
        summary["qualified"] = all(result["qualified"] for result in summary["runs"])
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        save(out / "analysis.json", summary)
    print(f"Production time APIs qualified={summary['qualified']}: {out}")


def footprint(out):
    firmware = out / "firmware"
    sections = {}
    for line in (firmware / "image-size.txt").read_text().splitlines():
        match = re.fullmatch(r"\s*(\.[\w.]+)\s+(\d+)\s+(\d+)\s*", line)
        if match:
            sections[match[1]] = int(match[2])
    if not {".text", ".data", ".bss"} <= sections.keys():
        raise ValueError("missing linked section sizes")
    return {"linked_flash_bytes": sum(sections.get(n, 0) for n in
                                     (".text", ".rodata", ".ARM.extab", ".ARM.exidx", ".data")),
            "unpadded_image_bytes": (firmware / "nuttx/nuttx.bin").stat().st_size,
            "static_ram_bytes": sections[".data"] + sections[".bss"],
            "sections": sections}


def compare(args):
    paths = [args.baseline.resolve(), args.controller.resolve()]
    records = [checked_build(path) for path in paths]
    if [r["variant"] for r in records] != ["baseline", "controller"]:
        raise ValueError("expected baseline then controller builds")
    for key in ("sources_after", "compiler", "native_environment_sha256"):
        if records[0][key] != records[1][key]:
            raise ValueError(f"comparison differs in {key}")
    for key in ("nxrs", "nuttx", "nuttx-apps"):
        if records[0]["identities"][key] != records[1]["identities"][key]:
            raise ValueError(f"comparison differs in {key} revision")
    if records[0]["artifacts"]["firmware/nuttx/.config"] != records[1]["artifacts"]["firmware/nuttx/.config"]:
        raise ValueError("comparison requires the same effective NuttX configuration")
    values = [footprint(path) for path in paths]
    report = {"schema": 1, "profile": PROFILE,
              "builds": [str(path) for path in paths],
              "build_sha256": [digest(path / "build.json") for path in paths],
              "baseline": values[0], "controller": values[1],
              "delta": {key: values[1][key] - values[0][key] for key in
                        ("linked_flash_bytes", "unpadded_image_bytes", "static_ram_bytes")},
              "scope": "Complete Rust qualification image delta, including enabled contract assertions; not an isolated crate cost.",
              "runtime_heap_stack_measured": False}
    if args.out.exists():
        raise ValueError("comparison output already exists")
    save(args.out, report)
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compile_ = commands.add_parser("build")
    compile_.add_argument("--out", type=Path, required=True)
    compile_.add_argument("--variant", choices=("baseline", "controller"), required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--build", type=Path, required=True)
    execute.add_argument("--repeat", type=int, choices=(1, 2, 3), default=3)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--baseline", type=Path, required=True)
    comparison.add_argument("--controller", type=Path, required=True)
    comparison.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    {"build": build, "run": run, "compare": compare}[args.command](args)


if __name__ == "__main__":
    main()
