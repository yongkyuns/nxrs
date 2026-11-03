#!/usr/bin/env python3
"""Build an isolated, uninstrumented event-path image; never flash or qualify it.

The std source, scheduler configuration and firmware builder remain the #252
implementations. This fixture owns only its application selection and receipt.
Every invocation requires a new output directory and private std/target cache.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import sys
import tarfile
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = ROOT / "tests/std-channel-perf"
SPEC = importlib.util.spec_from_file_location("event_path_std_build", BASE / "build.py")
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)
private_std = baseline.private_std

PACKAGE = "nxrs-event-path-perf"
BINARY = "event-path-perf"
COMMAND = "event_path_perf"
MANIFEST = HERE / "rust/Cargo.toml"
LOCK = ROOT / "Cargo.lock"
PLATFORMS = baseline.PLATFORMS
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
REGISTRY_PINS = {
    "crossbeam-channel": (
        "0.5.17", "98b0cc327b5bc766e7fda9c9260cc0fa81b43a8e240440422dff70788e3f9ef1"),
    "crossbeam-utils": (
        "0.8.23", "a31eee39dddec8330830986fcd7625edb5a24ec90ea038215273bbc3adb08ac6"),
}

# Keep P0's implementation rather than a divergent copy of its configuration,
# compiler, artifact-path and selected-std checks.
run = baseline.run
digest = baseline.digest
relative = baseline.relative
validate_configuration = baseline.validate_configuration
verify_selected_std = baseline.verify_selected_std
resolve_platform = baseline.resolve_platform


def inventory(profile: dict) -> dict:
    files = dict(baseline.inventory(profile))
    # P0's Rust application is not compiled into this fixture. Its reusable
    # native.rs is, while build.py/prepare-std.py/native.c remain build inputs.
    prefix = "tests/std-channel-perf/rust/"
    files = {name: value for name, value in files.items()
             if not name.startswith(prefix) or name.endswith("/src/native.rs")}
    paths = [ROOT / ".cargo/config.toml", ROOT / "platform/hal-build.rs",
             ROOT / "tools/platform_config.py", ROOT / "driver/acquisition.rs",
             ROOT / "driver/mock_acquisition.rs"]
    for directory in (HERE, ROOT / "shared/messaging", ROOT / "hal/imu"):
        paths.extend(p for p in directory.rglob("*")
                     if p.is_file() and p.suffix in (".rs", ".toml", ".py", ".c", ".h")
                     and not set(p.relative_to(directory).parts) & {"target", "__pycache__"})
    for path in sorted(set(paths)):
        files[path.relative_to(ROOT).as_posix()] = digest(path)
    return dict(sorted(files.items()))


def build_environment(out: Path, app_opt_level: str, original: dict,
                      clock_source: str = "baseline") -> dict:
    if original.get("NXRS_STD_CHANNEL_HOOK_STAGE", "0") != "0":
        raise ValueError("event-path images refuse an instrumented std hook stage")
    extra = ("CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER")
    if any(original.get(key) for key in extra):
        raise ValueError("unset competing Cargo compiler wrapper overrides")
    env = baseline.build_environment(out, "latency", app_opt_level, original, clock_source)
    env.pop("NXRS_STD_CHANNEL_BUILD_ID", None)
    env.pop("NXRS_EVENT_PATH_BUILD_ID", None)
    env["CARGO_TARGET_DIR"] = str(out / "firmware/cargo")
    return env


def firmware_command(platform: str, out: Path, kernel_optimization: str,
                     clock_source: str = "baseline") -> list:
    argv = ["bash", ROOT / "tools/build-nuttx-std-app.sh",
            "--app-manifest", MANIFEST, "--app-package", PACKAGE, "--bin", BINARY,
            "--command", COMMAND, "--priority", "100", "--stack-size", "65536",
            "--platform", platform, "--out", out / "firmware", "--abi-profile", "active",
            "--target-c-source", BASE / "native.c",
            "--target-c-source", HERE / "native-timer.c"]
    if kernel_optimization == "o2":
        argv += ["--kconfig-disable", "DEBUG_NOOPT", "--kconfig-disable", "DEBUG_FULLOPT",
                 "--kconfig-enable", "DEBUG_CUSTOMOPT"]
    if clock_source == "native-counter" and platform == "pico2-mock":
        argv += ["--kconfig-enable", "ARCH_PERF_EVENTS"]
    return argv


def locked_packages(manifest: Path = MANIFEST, lock: Path = LOCK) -> dict:
    declaration = tomllib.loads(manifest.read_text())["dependencies"]["crossbeam-channel"]
    if declaration != "=0.5.17":
        raise ValueError("event-path requires the exact registry Crossbeam =0.5.17 declaration")
    packages = tomllib.loads(lock.read_text()).get("package", [])
    selected = {}
    for name, (version, checksum) in REGISTRY_PINS.items():
        matches = [package for package in packages if package.get("name") == name]
        if (len(matches) != 1 or matches[0].get("version") != version
                or matches[0].get("source") != REGISTRY
                or matches[0].get("checksum") != checksum):
            raise ValueError(f"unexpected locked registry identity for {name}")
        selected[name] = matches[0]
    return selected


def registry_source(package: dict, locked: dict) -> dict:
    """Bind unpacked bytes to the checksum-pinned downloaded crate, without extraction."""
    for key in ("name", "version", "source"):
        if package.get(key) != locked.get(key):
            raise ValueError(f"resolved registry identity mismatch: {locked['name']}")
    manifest = Path(package["manifest_path"]).resolve(strict=True)
    source = manifest.parent
    directory_name = f"{locked['name']}-{locked['version']}"
    if (source.name != directory_name or source.parent.parent.name != "src"
            or source.parent.parent.parent.name != "registry"):
        raise ValueError("Crossbeam must resolve to a downloaded registry source, not a local patch")
    archive = source.parent.parent.parent / "cache" / source.parent.name / f"{directory_name}.crate"
    if not archive.is_file() or digest(archive) != locked["checksum"]:
        raise ValueError("Crossbeam registry archive does not match Cargo.lock checksum")
    files = {}
    with tarfile.open(archive, "r:gz") as contents:
        for member in contents:
            path = PurePosixPath(member.name)
            if (not path.parts or path.parts[0] != directory_name
                    or path.is_absolute() or ".." in path.parts):
                raise ValueError("invalid registry archive member path")
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) < 2:
                raise ValueError("registry archive contains a non-regular source")
            name = PurePosixPath(*path.parts[1:]).as_posix()
            if name in files:
                raise ValueError("registry archive contains a duplicate source")
            payload = contents.extractfile(member).read()
            actual = source / name
            if (actual.is_symlink() or not actual.is_file()
                    or not actual.resolve().is_relative_to(source)
                    or actual.read_bytes() != payload):
                raise ValueError(f"registry source bytes differ from pinned archive: {name}")
            files[name] = hashlib.sha256(payload).hexdigest()
    # Cargo owns these unpack/cache markers; they are not source files.
    ignored = {".cargo-ok", ".cargo-checksum.json"}
    actual_files = {p.relative_to(source).as_posix() for p in source.rglob("*")
                    if p.is_file() or p.is_symlink()}
    if actual_files - ignored != files.keys():
        raise ValueError("registry source contains missing or additional files")
    if "src/lib.rs" not in files or "Cargo.toml" not in files:
        raise ValueError("registry source omits its library or manifest")
    return {
        "package_id": package["id"], "name": locked["name"], "version": locked["version"],
        "source": locked["source"], "registry_checksum": locked["checksum"],
        "archive_file": str(archive), "manifest_path": str(manifest),
        "source_root": str(source), "source_sha256": dict(sorted(files.items())),
        "source_tree_sha256": private_std.digest(json.dumps(
            files, sort_keys=True, separators=(",", ":")).encode()),
        "uninstrumented_archive_match": True,
    }


def crossbeam_sources(metadata: dict, manifest: Path = MANIFEST, lock: Path = LOCK) -> dict:
    locked = locked_packages(manifest, lock)
    packages = {package["id"]: package for package in metadata["packages"]}
    roots = [package for package in packages.values()
             if Path(package["manifest_path"]).resolve() == manifest.resolve()]
    if len(roots) != 1 or roots[0]["name"] != PACKAGE:
        raise ValueError("Cargo metadata does not identify the event-path package")
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}

    def dependency(parent: dict, name: str) -> dict:
        selected = [dep["pkg"] for dep in nodes[parent["id"]]["deps"]
                    if dep["name"] == name.replace("-", "_")]
        if len(selected) != 1 or selected[0] not in packages:
            raise ValueError(f"ambiguous or missing selected dependency: {name}")
        return packages[selected[0]]

    channel = dependency(roots[0], "crossbeam-channel")
    utilities = dependency(channel, "crossbeam-utils")
    return {
        "schema": "event-path-registry-sources-v1",
        "packages": [registry_source(package, locked[package["name"]])
                     for package in (channel, utilities)],
    }


def cargo_metadata(prepared: Path, sdk: str, env: dict) -> dict:
    # Match the generic firmware builder's Cargo choice, including ESP's
    # stable Cargo + the prepared ESP rustc. No target build occurs here.
    cargo_sdk = "1.90.0" if sdk == "esp-1.90.0.0" else sdk
    cargo = run(["rustup", "which", "--toolchain", cargo_sdk, "cargo"], env=env)
    metadata_env = dict(env)
    metadata_env["RUSTC"] = str(prepared / "toolchain/bin/rustc")
    metadata_env["CARGO_BUILD_RUSTC"] = metadata_env["RUSTC"]
    metadata_env["RUSTC_BOOTSTRAP"] = "1"
    command = [cargo, "metadata", "--locked", "--format-version", "1",
               "--manifest-path", str(MANIFEST)]
    print("+", " ".join(command), flush=True)
    # Cargo writes registry/download diagnostics to stderr even on success.
    # Preserve both streams, but parse only its machine-readable stdout.
    result = subprocess.run(command, cwd=ROOT, env=metadata_env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (prepared.parent / "cargo-metadata.log").write_text(
        f"exit status: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    if result.returncode:
        print(result.stderr[-6000:], file=sys.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, command, output=result.stdout, stderr=result.stderr)
    return json.loads(result.stdout)


def verify_compiled_sources(firmware: Path, sources: dict) -> None:
    messages = [json.loads(line) for line in (firmware / "cargo-messages.jsonl").read_text().splitlines()
                if line.strip()]
    for package in sources["packages"]:
        artifacts = [message for message in messages
                     if message.get("reason") == "compiler-artifact"
                     and message.get("package_id") == package["package_id"]
                     and "lib" in message.get("target", {}).get("kind", [])]
        expected = Path(package["source_root"]) / "src/lib.rs"
        if (len(artifacts) != 1
                or Path(artifacts[0]["target"]["src_path"]).resolve() != expected.resolve()):
            raise ValueError(f"Cargo did not compile the pinned {package['name']} source")


def build(args) -> dict:
    out = args.out.absolute()
    if args.platform not in PLATFORMS:
        raise ValueError("unknown event-path platform")
    if args.clock_source not in ("baseline", "native-counter"):
        raise ValueError("unknown clock source")
    if (args.clock_source == "native-counter"
            and args.platform not in ("pico2-mock", "esp32s3-service-footprint")):
        raise ValueError("native-counter requires a supported physical-board profile")
    if out.exists():
        raise ValueError("build output must be new; images may not reuse a directory")
    profile = resolve_platform(args.platform, ROOT)
    sdk = baseline.sdk_for(profile)
    env = build_environment(out, args.app_opt_level, os.environ, args.clock_source)
    locked_packages()
    source = (args.source_sysroot.resolve(strict=True) if args.source_sysroot
              else baseline.discover_source(profile, sdk, env))
    before = inventory(profile)
    out.mkdir(parents=True)
    record = {
        "schema": "event-path-build-v1", "status": "failed", "failure": "build incomplete",
        "mode": "uninstrumented", "hook_stage": 0,
        "package": PACKAGE, "binary": BINARY, "command": COMMAND,
        "platform": args.platform, "target": profile["target"], "sdk": sdk,
        "runtime_kind": "unmeasured", "source_sha256": before,
        "clock_request": args.clock_source,
        "clock_frequency_claim": "configuration-derived; physical timing requires P0 calibration",
        "source_revision": run(["git", "rev-parse", "HEAD"]),
        "source_dirty": bool(run(["git", "status", "--porcelain"])),
        "build_nonce": secrets.token_hex(16),
        "kernel_revision": run(["git", "rev-parse", ":external/nuttx"]),
        "apps_revision": run(["git", "rev-parse", ":external/nuttx-apps"]),
        "optimization": {"rust_opt_level": args.app_opt_level,
                         "kernel_request": args.kernel_optimization},
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
        hook = private_std.prepare(source, prepared, sdk, "latency")
        if (hook.get("hook_stage") != 0 or hook.get("channel_instrumented") is not False
                or hook["original"]["sha256"] != hook["selected"]["sha256"]):
            raise ValueError("prepared event-path std is not uninstrumented")
        metadata = cargo_metadata(prepared, sdk, env)
        registry = crossbeam_sources(metadata)
        (out / "crossbeam-source.json").write_text(json.dumps(registry, indent=2) + "\n")
        record.update(
            std_array_original_sha256=hook["original"]["sha256"],
            std_array_sha256=hook["selected"]["sha256"],
            std_library_tree_sha256=hook["library_tree_sha256"],
            compiler_version=hook["compiler"]["version"], compiler_identity=hook["compiler"],
            hook_receipt="prepared/std-channel-hook.json",
            crossbeam_source_receipt="crossbeam-source.json",
            crossbeam=registry,
        )
        identity = {key: record[key] for key in (
            "package", "binary", "command", "mode", "hook_stage", "platform", "target", "sdk",
            "source_sha256", "kernel_revision", "apps_revision", "optimization", "std_array_sha256",
            "std_library_tree_sha256", "compiler_identity", "crossbeam", "build_nonce",
            "build_environment", "clock_request",
        )}
        record["build_id"] = private_std.digest(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode())
        env["NXRS_EVENT_PATH_BUILD_ID"] = record["build_id"]
        command = firmware_command(args.platform, out, args.kernel_optimization, args.clock_source)
        record["build_command"] = [str(value) for value in command]
        run(command, env=env, log=out / "build.log")
        firmware = out / "firmware"
        verify_selected_std(firmware, prepared, "latency")
        verify_compiled_sources(firmware, registry)
        if crossbeam_sources(metadata) != registry:
            raise ValueError("Crossbeam registry sources changed during compilation")
        image = firmware / "nuttx" / profile["image"]
        elf = firmware / "nuttx/nuttx"
        config = firmware / "resolved.config"
        if not image.is_file() or image.stat().st_size == 0:
            raise ValueError("final firmware image is missing or empty")
        record["resolved_configuration"] = validate_configuration(
            config, args.kernel_optimization, args.clock_source, args.platform)
        record.update(
            image_file=relative(image, out), image_sha256=digest(image),
            elf_file=relative(elf, out), elf_sha256=digest(elf),
            config_file=relative(config, out), config_sha256=digest(config),
            native_provenance="firmware/provenance.txt", std_source_proof="firmware/std-source.json",
            abi_receipt="firmware/abi-report.json",
        )
        record["artifacts_sha256"] = {
            relative(path, out): digest(path) for path in (
                image, elf, config, firmware / "provenance.txt", firmware / "std-source.json",
                firmware / "std-patch.json", prepared / "std-channel-hook.json",
                prepared / "std-library-sources.json", firmware / "abi-report.json",
                firmware / "cargo-messages.jsonl", out / "crossbeam-source.json",
            )
        }
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
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-sysroot", type=Path,
                        help="original SDK; exact pinned std sources are still required")
    parser.add_argument("--app-opt-level", choices=("2", "3", "s", "z"), default="2")
    parser.add_argument("--kernel-optimization", choices=("baseline", "o2"), default="baseline")
    parser.add_argument("--clock-source", choices=("baseline", "native-counter"), default="baseline")
    args = parser.parse_args()
    try:
        record = build(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"event-path firmware build failed: {error}\n")
    print("EVENT_PATH_BUILD_COMPLETE", args.platform, record["image_sha256"])


if __name__ == "__main__":
    main()
