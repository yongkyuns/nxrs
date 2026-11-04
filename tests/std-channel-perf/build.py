#!/usr/bin/env python3
"""Build isolated std-channel qualification images with truthful source receipts.

This creates firmware only. It neither flashes a board nor claims measurements.
Use a fresh output directory for each mode, platform and optimization profile.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import simpleboot_digest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location("std_channel_private_std", HERE / "prepare-std.py")
private_std = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(private_std)
INPUT_SPEC = importlib.util.spec_from_file_location("std_channel_build_inputs", HERE / "build_inputs.py")
build_inputs = importlib.util.module_from_spec(INPUT_SPEC)
INPUT_SPEC.loader.exec_module(build_inputs)
sys.path.insert(0, str(ROOT / "tools"))


def resolve_platform(name, root=ROOT):
    # Import only for a build/profile query. Offline receipt replay must not
    # compile the Rust frontend or contact a registry merely to import helpers.
    from platform_config import resolve
    return resolve(name, root)

PLATFORMS = (
    "mps2-an521-mock", "pico2-mock", "esp32s3-service-footprint", "esp32s3-qemu-mock",
)
PACKAGE = "nxrs-std-channel-perf"
BINARY = "std-channel-perf"
COMMAND = "std_channel_perf"
MANIFEST = HERE / "rust/Cargo.toml"
UNSAFE_OVERRIDES = (
    "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_ENCODED_RUSTFLAGS",
    "NXRS_CARGO_CONFIG", "NXRS_SHARED_NUTTX_STD_SYSROOT", "NXRS_SHARED_CARGO_TARGET_DIR",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, *, cwd=ROOT, env=None, log: Path | None = None) -> str:
    command = [str(x) for x in argv]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if log is not None:
        log.write_text(result.stdout)
    if result.returncode:
        # Keep full logs on disk, but make an actionable tail visible to callers.
        print(result.stdout[-6000:], file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout)
    return result.stdout.strip()


def relative(path: Path, root: Path) -> str:
    path = path.resolve(strict=True)
    if not path.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"artifact escapes build directory: {path}")
    return path.relative_to(root.resolve()).as_posix()


def inventory(profile: dict) -> dict:
    files = [HERE / "build.py", HERE / "prepare-std.py", HERE / "native.c",
             HERE / "simpleboot_digest.py", HERE / "inspect_esp_image.py",
             ROOT / "Cargo.toml", ROOT / "Cargo.lock",
             ROOT / "tools/build-nuttx-std-app.sh", ROOT / "tools/platform_config.py",
             ROOT / "tools/check-deployment.py", ROOT / "tools/apply-nuttx-patches.py",
             ROOT / "tests/nuttx-std/prepare-std.py", ROOT / "tests/nuttx-std/check-abi.py",
             ROOT / "tests/nuttx-std/link.py"]
    files += build_inputs.files(ROOT)
    files += list((HERE / "rust").rglob("*.rs")) + [MANIFEST]
    files += list(HERE.glob("*.h"))
    files += [Path(p) for p in profile["sources"]]
    files += [p for p in (ROOT / "platform/nuttx/std-app").iterdir() if p.is_file()]
    for component in ("nuttx", "nuttx-apps", "rust-std", "libc"):
        files += [p for p in (ROOT / "upstream" / component).rglob("*") if p.is_file()]
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(files))}


def sdk_for(profile: dict) -> str:
    if profile["target"] == "thumbv8m.main-nuttx-eabi":
        return "nightly-2025-09-15"
    if profile["target"] == "xtensa-esp32s3-nuttx":
        return "esp-1.90.0.0"
    raise ValueError("unsupported target for std-channel qualification")


def discover_source(profile: dict, sdk: str, env: dict) -> Path:
    if sdk == "nightly-2025-09-15":
        compiler = run(["rustup", "which", "--toolchain", sdk, "rustc"], env=env)
        return Path(run([compiler, "--print", "sysroot"], env=env)).resolve(strict=True)
    tools = Path(env.get("NXRS_QEMU_TOOLS_DIR", str(ROOT / "target/qemu-tools")))
    environment = tools.resolve() / "environment.sh"
    if not environment.is_file():
        raise ValueError("missing pinned ESP tool environment; run tools/install-qemu-tools.sh")
    # The existing pinned tools script owns these paths; do not parse shell as data.
    source = run(["bash", "-c", 'source "$1"; "$RUSTC" --print sysroot',
                  "std-channel-sdk", environment], env=env)
    return Path(source).resolve(strict=True)


def build_environment(out: Path, mode: str, app_opt_level: str, original: dict,
                      clock_source: str = "baseline") -> dict:
    found = [key for key in UNSAFE_OVERRIDES if original.get(key)]
    if found:
        raise ValueError("unset competing compiler/source overrides: " + ", ".join(found))
    env = dict(original)
    # The generic builder sets RUSTFLAGS itself; never let an encoded version
    # silently override it or share a Cargo cache across witness/latency modes.
    env.pop("RUSTFLAGS", None)
    env["NXRS_SHARED_NUTTX_STD_SYSROOT"] = str(out / "prepared/toolchain")
    env["NXRS_SHARED_CARGO_TARGET_DIR"] = str(out / "firmware/cargo")
    env["NXRS_STD_CHANNEL_HOOK_STAGE"] = str(private_std.MODES[mode])
    env["NXRS_STD_CHANNEL_CLOCK_SOURCE"] = clock_source
    env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = app_opt_level
    return env


def firmware_command(platform: str, out: Path, kernel_optimization: str,
                     clock_source: str = "baseline") -> list:
    argv = ["bash", ROOT / "tools/build-nuttx-std-app.sh",
            "--app-manifest", MANIFEST, "--app-package", PACKAGE, "--bin", BINARY,
            "--command", COMMAND, "--priority", "100", "--stack-size", "65536",
            "--platform", platform, "--out", out / "firmware", "--abi-profile", "active",
            "--target-c-source", HERE / "native.c"]
    if kernel_optimization == "o2":
        # DEBUG_CUSTOMOPT's pinned default is -O2; verify the resolved value below.
        # Assertions/diagnostics stay at their platform settings.
        argv += ["--kconfig-disable", "DEBUG_NOOPT", "--kconfig-disable", "DEBUG_FULLOPT",
                 "--kconfig-enable", "DEBUG_CUSTOMOPT"]
    if clock_source == "native-counter" and platform == "pico2-mock":
        argv += ["--kconfig-enable", "ARCH_PERF_EVENTS"]
    return argv


def config_values(config: Path) -> dict[str, str]:
    result = {}
    for line in config.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            result[line[2:-11]] = "n"
    return result


def validate_configuration(config: Path, kernel_optimization: str,
                           clock_source: str = "baseline", platform: str = "") -> dict:
    values = config_values(config)
    if values.get("CONFIG_BUILD_FLAT") != "y" or values.get("CONFIG_SMP") == "y":
        raise ValueError("qualification requires the selected flat, single-core kernel")
    if values.get("CONFIG_DISABLE_PTHREAD") == "y":
        raise ValueError("pthreads unexpectedly disabled")
    if kernel_optimization == "o2" and (
        values.get("CONFIG_DEBUG_CUSTOMOPT") != "y"
        or values.get("CONFIG_DEBUG_OPTLEVEL") != '"-O2"'
    ):
        raise ValueError("requested kernel -O2 was not resolved exactly")
    if (clock_source == "native-counter" and platform == "pico2-mock"
            and values.get("CONFIG_ARCH_PERF_EVENTS") != "y"):
        raise ValueError("requested Pico 2 architecture counter was not enabled")
    names = (
        "CONFIG_BUILD_FLAT", "CONFIG_SMP", "CONFIG_RR_INTERVAL", "CONFIG_USEC_PER_TICK",
        "CONFIG_SCHED_TICKLESS", "CONFIG_SYSTEM_TIME64", "CONFIG_CLOCK_MONOTONIC",
        "CONFIG_DEBUG_NOOPT", "CONFIG_DEBUG_CUSTOMOPT", "CONFIG_DEBUG_FULLOPT",
        "CONFIG_DEBUG_OPTLEVEL", "CONFIG_DEBUG_FEATURES", "CONFIG_DEBUG_ASSERTIONS",
        "CONFIG_DEBUG_SYMBOLS", "CONFIG_STACK_COLORATION", "CONFIG_MM_BACKTRACE",
        "CONFIG_MM_BACKTRACE_DEFAULT", "CONFIG_SCHED_CRITMONITOR", "CONFIG_SCHED_IRQMONITOR",
        "CONFIG_SCHED_INSTRUMENTATION", "CONFIG_PRIORITY_INHERITANCE",
        "CONFIG_PRIORITY_PROTECT", "CONFIG_CANCELLATION_POINTS", "CONFIG_PTHREAD_STACK_DEFAULT",
        "CONFIG_PREALLOC_MQ_MSGS", "CONFIG_PREALLOC_MQ_IRQ_MSGS", "CONFIG_MQ_MAXMSGSIZE",
        "CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ", "CONFIG_ESP32S3_SPIRAM",
        "CONFIG_ARCH_HAVE_PERF_EVENTS", "CONFIG_ARCH_PERF_EVENTS",
        "CONFIG_ARCH_PERF_COUNT_BITWIDTH", "CONFIG_PERF_OVERFLOW_CORRECTION", "CONFIG_PM",
    )
    return {key: values.get(key, "absent") for key in names}


def verify_selected_std(firmware: Path, prepared: Path, mode: str) -> dict:
    hook = private_std.validate_receipt(prepared, mode)
    proof = json.loads((firmware / "std-source.json").read_text())
    expected = (prepared / "toolchain" / private_std.LIBRARY / "std/src/lib.rs").resolve()
    if (proof.get("selected_source_matches") is not True
            or Path(proof.get("std_source", "")).resolve() != expected
            or Path(proof.get("rustc", "")).resolve()
            != (prepared / "toolchain/bin/rustc").resolve()):
        raise ValueError("Cargo did not compile the selected private std/compiler")
    ledger = json.loads((firmware / "std-patch.json").read_text())
    if ledger.get("channel_qualification") != hook:
        raise ValueError("firmware private-std ledger does not match selected bytes")
    return hook


def build(args) -> dict:
    out = args.out.absolute()
    if args.platform not in PLATFORMS or args.mode not in private_std.MODES:
        raise ValueError("unknown platform or image mode")
    if args.clock_source not in ("baseline", "native-counter"):
        raise ValueError("unknown clock source")
    if (args.clock_source == "native-counter"
            and args.platform not in ("pico2-mock", "esp32s3-service-footprint")):
        raise ValueError("native-counter requires a supported physical-board profile; use baseline for QEMU")
    boot_profile = getattr(args, "boot_image_profile", "platform-default")
    if boot_profile not in ("platform-default", simpleboot_digest.PROFILE):
        raise ValueError("unknown boot image profile")
    if boot_profile != "platform-default" and args.platform != "esp32s3-service-footprint":
        raise ValueError("digest candidate requires physical ESP32-S3 profile")
    if out.exists():
        raise ValueError("build output must be new; modes may not reuse a directory")
    profile = resolve_platform(args.platform, ROOT)
    sdk = sdk_for(profile)
    env = build_environment(out, args.mode, args.app_opt_level, os.environ, args.clock_source)
    source = args.source_sysroot.resolve(strict=True) if args.source_sysroot else discover_source(profile, sdk, os.environ)
    before = inventory(profile)
    out.mkdir(parents=True)
    record = {
        "schema": "std-channel-build-v1", "status": "failed", "failure": "build incomplete",
        "mode": args.mode, "hook_stage": private_std.MODES[args.mode],
        "platform": args.platform, "target": profile["target"], "sdk": sdk,
        "runtime_kind": "unmeasured", "source_sha256": before,
        "clock_request": args.clock_source,
        "boot_image_profile": boot_profile,
        "clock_frequency_claim": "configuration-derived; physical capture requires independent clock verification",
        "source_revision": run(["git", "rev-parse", "HEAD"]),
        "source_dirty": bool(run(["git", "status", "--porcelain"])),
        # This is a capture identity, not a promise that two rebuilds produce
        # identical bits. Actual ELF/flash hashes below establish binary identity.
        "build_nonce": secrets.token_hex(16),
        "kernel_revision": run(["git", "rev-parse", ":external/nuttx"]),
        "apps_revision": run(["git", "rev-parse", ":external/nuttx-apps"]),
        "optimization": {"rust_opt_level": args.app_opt_level,
                         "kernel_request": args.kernel_optimization},
        "command": COMMAND,
        "measurement_claim": "none; build receipt is not evidence of execution or performance",
        "build_environment": {key: env[key] for key in (
            "CARGO_PROFILE_RELEASE_OPT_LEVEL", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
            "CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS", "CARGO_PROFILE_RELEASE_OVERFLOW_CHECKS",
            "CARGO_INCREMENTAL", "NXRS_NUTTX_DIAGNOSTIC_UNWIND", "NXRS_NUTTX_BUILD_JOBS",
            "SOURCE_DATE_EPOCH",
        ) if key in env},
    }
    try:
        prepared = out / "prepared"
        hook = private_std.prepare(source, prepared, sdk, args.mode)
        record.update(
            std_array_original_sha256=hook["original"]["sha256"],
            std_array_sha256=hook["selected"]["sha256"],
            std_library_tree_sha256=hook["library_tree_sha256"],
            compiler_version=hook["compiler"]["version"], compiler_identity=hook["compiler"],
            hook_receipt="prepared/std-channel-hook.json",
        )
        build_identity = {key: record[key] for key in (
            "mode", "hook_stage", "platform", "target", "sdk", "source_sha256",
            "kernel_revision", "apps_revision", "optimization", "std_array_sha256",
            "std_library_tree_sha256", "compiler_identity", "build_nonce", "build_environment", "clock_request",
            "boot_image_profile",
        )}
        record["build_id"] = private_std.digest(json.dumps(
            build_identity, sort_keys=True, separators=(",", ":")).encode())
        env["NXRS_STD_CHANNEL_BUILD_ID"] = record["build_id"]
        command = firmware_command(args.platform, out, args.kernel_optimization, args.clock_source)
        record["build_command"] = [str(x) for x in command]
        run(command, env=env, log=out / "build.log")
        firmware = out / "firmware"
        verify_selected_std(firmware, prepared, args.mode)
        image = firmware / "nuttx" / profile["image"]
        elf = firmware / "nuttx/nuttx"
        config = firmware / "resolved.config"
        if not image.is_file() or image.stat().st_size == 0:
            raise ValueError("final firmware image is missing or empty")
        record["resolved_configuration"] = validate_configuration(
            config, args.kernel_optimization, args.clock_source, args.platform)
        digest_artifacts = []
        if boot_profile == simpleboot_digest.PROFILE:
            source_image = image
            image, digest_report = simpleboot_digest.prepare(firmware, source_image)
            record["simpleboot_digest_report"] = relative(digest_report, out)
            record["unmodified_source_image"] = relative(source_image, out)
            digest_artifacts = [source_image, digest_report, firmware / "nuttx-patches.json"]
        record.update(
            image_file=relative(image, out), image_sha256=digest(image),
            elf_file=relative(elf, out), elf_sha256=digest(elf),
            config_file=relative(config, out), config_sha256=digest(config),
            native_provenance="firmware/provenance.txt", std_source_proof="firmware/std-source.json",
            abi_receipt="firmware/abi-report.json",
        )
        record["artifacts_sha256"] = {
            relative(p, out): digest(p) for p in (
                image, elf, config, firmware / "provenance.txt", firmware / "std-source.json",
                firmware / "std-patch.json", prepared / "std-channel-hook.json",
                prepared / "std-library-sources.json", firmware / "abi-report.json",
            )
        }
        record["artifacts_sha256"].update({
            relative(path, out): digest(path) for path in digest_artifacts
        })
        if inventory(profile) != before:
            raise ValueError("firmware sources changed during compilation")
        record["status"] = "complete"
        record["failure"] = None
    except Exception as error:
        record["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (out / "build.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--mode", required=True, choices=tuple(private_std.MODES))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-sysroot", type=Path,
                        help="original SDK location; exact pinned sources are still required")
    parser.add_argument("--app-opt-level", choices=("2", "3", "s", "z"), default="2")
    parser.add_argument("--kernel-optimization", choices=("baseline", "o2"), default="baseline")
    parser.add_argument("--clock-source", choices=("baseline", "native-counter"), default="baseline",
                        help="native-counter is explicit for physical ESP32-S3/Pico 2; baseline is suitable for functional QEMU runs")
    parser.add_argument("--boot-image-profile",
                        choices=("platform-default", simpleboot_digest.PROFILE),
                        default="platform-default",
                        help="opt-in ESP32-S3 digest candidate; requires rebuilt loader")
    args = parser.parse_args()
    try:
        record = build(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"std-channel firmware build failed: {error}\n")
    print("STD_CHANNEL_BUILD_COMPLETE", args.mode, args.platform, record["image_sha256"])


if __name__ == "__main__":
    main()
