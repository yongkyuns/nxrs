#!/usr/bin/env python3
"""Build a private progress image, without editing P0 or Cargo's cache.

The staged workspace and source patches are diagnostic inputs. Ordinary nxrs
dependencies, the root lockfile, and the installed SDK remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tomllib
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


boot_image = module("channel_progress_boot_image", HERE / "boot_image.py")
baseline = module("channel_progress_baseline_build", HERE.parent / "std-channel-perf/build.py")
private = module("channel_progress_private_crossbeam", HERE / "prepare-crossbeam.py")
MODES = {
    "direct-reserved": "send-reserved", "direct-published": "send-published",
    "select-reserved": "send-reserved", "select-published": "send-published",
    "send-reserved": "recv-reserved", "send-reclaimed": "recv-reclaimed",
    "std-mutex-suite": "clean",
    "fifo-channel-suite": "fifo-ordered",
}
PACKAGE = "nxrs-channel-progress"
BINARY = "channel-progress"
COMMAND = "channel_progress"
SHARED_FILES = ("native.c", "native.h", "rust/src/native.rs")
UNSAFE_OVERRIDES = (*baseline.UNSAFE_OVERRIDES, "CARGO_BUILD_RUSTC", "RUSTC")
IDENTITY_FIELDS = (
    "mode", "hook_stage", "platform", "target", "sdk", "source_sha256",
    "kernel_revision", "apps_revision", "build_nonce", "crossbeam_receipt",
    "compiler_identity", "std_library_tree_sha256", "staged_inputs_sha256",
)


def identity_fields(mode, receipt=None):
    # Historical receipts retain their frozen identity. Every new build binds
    # its explicit image profile before compiling the runtime build ID.
    fields = IDENTITY_FIELDS + (("fifo_kernel_source_sha256",)
                                if mode == "fifo-channel-suite" else ())
    return fields + (("boot_image_profile",)
                     if receipt is not None and "boot_image_profile" in receipt else ())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def crate_archives(cache):
    """Fetch only lockfile-pinned release archives; verify even cached bytes."""
    cache.mkdir(parents=True, exist_ok=True)
    locked = tomllib.loads((ROOT / "Cargo.lock").read_text())["package"]
    result = []
    for name in ("crossbeam-channel", "crossbeam-utils"):
        choices = [p for p in locked if p["name"] == name]
        if len(choices) != 1:
            raise ValueError("ambiguous Crossbeam lockfile package")
        package = choices[0]
        path = cache / f'{name}-{package["version"]}.crate'
        if not path.exists():
            temporary = path.with_suffix(".download-" + secrets.token_hex(8))
            try:
                url = f'https://static.crates.io/crates/{name}/{path.name}'
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("crate archive size limit")
                temporary.write_bytes(data)
                if sha(temporary) != package["checksum"]:
                    raise ValueError("downloaded Crossbeam archive checksum mismatch")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        if sha(path) != package["checksum"]:
            raise ValueError("cached Crossbeam archive checksum mismatch")
        result.append(path)
    return result


def inventory(profile):
    # Baseline's common SDK/build helpers are reused read-only. Exclude its
    # latency/capture/analyzer sources, which belong to the parallel P0 stream.
    files = [ROOT / "Cargo.toml", ROOT / "Cargo.lock"]
    files += baseline.build_inputs.files(ROOT)
    files += [HERE.parent / "std-channel-perf" / p for p in SHARED_FILES]
    files += [HERE.parent / "std-channel-perf" / p for p in (
        "build.py", "prepare-std.py", "simpleboot_digest.py", "inspect_esp_image.py")]
    files += [p for p in HERE.rglob("*") if p.is_file() and
              p.suffix in (".rs", ".toml", ".py") and "__pycache__" not in p.parts]
    files += [ROOT / p for p in (
        "tools/build-nuttx-std-app.sh", "tools/platform_config.py",
        "tools/check-deployment.py", "tools/apply-nuttx-patches.py", "tools/nuttx-version.py",
        "platform/nuttx/init.py", "platform/nuttx/mcuboot.py",
        "tests/nuttx-std/prepare-std.py", "tests/nuttx-std/check-abi.py", "tests/nuttx-std/link.py",
    )]
    files += [Path(p) for p in profile["sources"]]
    files += [ROOT / "hal" / name / "Cargo.toml" for name in profile["hal"]]
    for directory in ("platform/nuttx/std-app", "upstream/nuttx", "upstream/nuttx-apps",
                      "upstream/rust-std", "upstream/libc"):
        files += [p for p in (ROOT / directory).rglob("*") if p.is_file()]
    if profile["target"] == "xtensa-esp32s3-nuttx":
        files += [ROOT / "tools/prepare-esp-hal-cache.sh"]
        files += [p for p in (ROOT / "upstream/esp-hal-3rdparty").rglob("*") if p.is_file()]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(files))}


def stage_workspace(out, profile):
    """A minimal new workspace allows a private patch lock without root edits."""
    workspace = out / "workspace"
    # The actual Rust deployment validator canonicalizes the HAL root even
    # when this profile declares no HAL providers. Preserve that empty root
    # without staging any concrete provider implementation.
    (workspace / "hal").mkdir(parents=True, exist_ok=True)
    manifest = workspace / "tests/channel-progress/rust/Cargo.toml"
    shutil.copytree(HERE / "rust", manifest.parent)
    for name in SHARED_FILES:
        source = HERE.parent / "std-channel-perf" / name
        target = workspace / "tests/std-channel-perf" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # Deployment validation resolves profiles from the manifest's workspace,
    # including the provider declarations in HAL manifests. Copy their exact
    # metadata, without adding any HAL implementation to this fixture graph.
    metadata = [Path(p) for p in profile["sources"]]
    metadata += [ROOT / "hal" / name / "Cargo.toml" for name in profile["hal"]]
    for source in metadata:
        target = workspace / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (workspace / "Cargo.toml").write_text('''[workspace]
resolver = "2"
members = ["tests/channel-progress/rust"]
[workspace.package]
edition = "2021"
version = "0.1.0"
[profile.dev]
panic = "abort"
[profile.release]
panic = "abort"
lto = false
codegen-units = 1
opt-level = 2
strip = "none"
debug = 1
overflow-checks = false
debug-assertions = false
''')
    return manifest


def build_environment(out, mode, original):
    if mode not in MODES:
        raise ValueError("unknown progress mode")
    found = [key for key in UNSAFE_OVERRIDES if original.get(key)]
    if found:
        raise ValueError("unset competing compiler/source overrides: " + ", ".join(found))
    env = dict(original)
    env.pop("RUSTFLAGS", None)
    env["NXRS_SHARED_NUTTX_STD_SYSROOT"] = str(out / "prepared/toolchain")
    env["NXRS_SHARED_CARGO_TARGET_DIR"] = str(out / "firmware/cargo")
    env["NXRS_CARGO_CONFIG"] = str(out / "crossbeam/cargo-config.toml")
    env["NXRS_CHANNEL_PROGRESS_HOOK_STAGE"] = str(private.MODES[MODES[mode]])
    env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = "2"
    env["CARGO_INCREMENTAL"] = "0"
    return env


def firmware_command(platform, out, manifest):
    argv = ["bash", ROOT / "tools/build-nuttx-std-app.sh",
            "--app-manifest", manifest, "--app-package", PACKAGE, "--bin", BINARY,
            "--command", COMMAND, "--priority", "100", "--stack-size", "65536",
            "--platform", platform, "--out", out / "firmware", "--abi-profile", "active",
            "--target-c-source", out / "workspace/tests/std-channel-perf/native.c"]
    if platform == "mps2-an521-mock":
        argv += ["--kconfig-integer", "CMSDK_UART0_TX_IRQ=48",
                 "--kconfig-integer", "CMSDK_UART0_RX_IRQ=49"]
    return argv


def cargo_source_proof(out, receipt):
    messages = [json.loads(line) for line in (out / "firmware/cargo-messages.jsonl").read_text().splitlines()]
    proof = {"schema": "channel-progress-cargo-sources-v1", "sources": {}}
    for name, item in receipt["crates"].items():
        artifacts = [m for m in messages if m.get("reason") == "compiler-artifact"
                     and m.get("target", {}).get("name") == name.replace("-", "_")
                     and "lib" in m.get("target", {}).get("kind", [])]
        if len(artifacts) != 1:
            raise ValueError(f"expected one compiled {name} artifact")
        artifact = artifacts[0]
        actual = Path(artifact["target"]["src_path"]).resolve(strict=True)
        expected = (out / "crossbeam" / item["source_directory"] / "src/lib.rs").resolve(strict=True)
        if actual != expected:
            raise ValueError(f"Cargo compiled the wrong {name} source")
        files = [Path(p) for p in artifact["filenames"] if Path(p).suffix in (".rlib", ".rmeta")]
        if not files or any(not p.is_file() or p.stat().st_size == 0 for p in files):
            raise ValueError(f"missing compiled {name} library")
        proof["sources"][name] = {
            "package_id": artifact["package_id"], "src_path": str(actual),
            "expected_src_path": str(expected), "source_matches": True,
            "artifact_sha256": {baseline.relative(p, out): sha(p) for p in files},
        }
    (out / "firmware/crossbeam-source.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


def build(args):
    out = args.out.absolute()
    if args.mode not in MODES or args.platform not in baseline.PLATFORMS:
        raise ValueError("unsupported progress case or platform")
    boot_profile = getattr(args, "boot_image_profile", boot_image.DEFAULT)
    boot_image.validate_profile(boot_profile, args.platform)
    if out.exists():
        raise ValueError("build output must be new; preserve earlier images")
    profile = baseline.resolve_platform(args.platform, ROOT)
    sdk = baseline.sdk_for(profile)
    env = build_environment(out, args.mode, os.environ)
    source = (args.source_sysroot.resolve(strict=True) if args.source_sysroot else
              baseline.discover_source(profile, sdk, os.environ))
    archives = crate_archives(args.crate_cache)
    before = inventory(profile)
    out.mkdir(parents=True)
    record = {
        "schema": "channel-progress-build-v1", "status": "failed", "failure": "build incomplete",
        "runtime_kind": "unmeasured", "mode": args.mode,
        "boot_image_profile": boot_profile,
        "hook_stage": private.MODES[MODES[args.mode]], "platform": args.platform,
        "target": profile["target"], "sdk": sdk, "source_sha256": before,
        "source_revision": baseline.run(["git", "rev-parse", "HEAD"]),
        "source_dirty": bool(baseline.run(["git", "status", "--porcelain"])),
        "kernel_revision": baseline.run(["git", "rev-parse", ":external/nuttx"]),
        "apps_revision": baseline.run(["git", "rev-parse", ":external/nuttx-apps"]),
        "build_nonce": secrets.token_hex(16), "command": COMMAND,
        "measurement_claim": "source-bound progress diagnostic, never timing evidence",
    }
    try:
        hooks = private.prepare(*archives, out / "crossbeam", MODES[args.mode])
        std = baseline.private_std.prepare(source, out / "prepared", sdk, "latency")
        if args.mode in {"std-mutex-suite", "fifo-channel-suite"}:
            primitive = module("channel_progress_std_sources", HERE / "mutex_analyze.py")
            primitive.validate_std_sources(json.loads(
                (out / "prepared/std-library-sources.json").read_text()), sdk)
        if args.mode == "fifo-channel-suite":
            kernel = module("channel_progress_fifo_sources", HERE / "fifo_sources.py")
            kernel.validate_std_sources(json.loads(
                (out / "prepared/std-library-sources.json").read_text()), sdk)
            if record["kernel_revision"] != kernel.KERNEL_REVISION:
                raise ValueError("FIFO scheduling requires the audited kernel revision")
            record["fifo_kernel_source_sha256"] = kernel.prepare_kernel(
                ROOT / "external/nuttx", out / "prepared/fifo-kernel")
        manifest = stage_workspace(out, profile)
        baseline.run(["cargo", "+1.90.0", "--config", out / "crossbeam/cargo-config.toml",
                      "generate-lockfile", "--offline", "--manifest-path", manifest], env=env)
        staged_inputs = {p.relative_to(out).as_posix(): sha(p) for p in
                         (out / "workspace").rglob("*") if p.is_file()}
        record.update(
            crossbeam_receipt=hooks, crossbeam_receipt_file="crossbeam/crossbeam-hook.json",
            compiler_identity=std["compiler"], std_library_tree_sha256=std["library_tree_sha256"],
            std_array_sha256=std["selected"]["sha256"],
            std_array_original_sha256=std["original"]["sha256"],
            staged_inputs_sha256=staged_inputs,
        )
        record["build_id"] = canonical({key: record[key] for key in identity_fields(args.mode, record)})
        env["NXRS_CHANNEL_PROGRESS_BUILD_ID"] = record["build_id"]
        command = firmware_command(args.platform, out, manifest)
        record["build_command"] = [str(p) for p in command]
        baseline.run(command, env=env, log=out / "build.log")
        baseline.verify_selected_std(out / "firmware", out / "prepared", "latency")
        private.validate_receipt(out / "crossbeam", MODES[args.mode])
        cargo_source_proof(out, hooks)
        firmware = out / "firmware"
        if args.mode == "fifo-channel-suite":
            # The private target tree is the one compiled against the resolved
            # configuration. Check it after all normal platform patches, not
            # just the submodule that supplied it.
            selected = kernel.verify_kernel(firmware / "nuttx")
            kernel.validate_retained_kernel(out / "prepared/fifo-kernel",
                                            record["fifo_kernel_source_sha256"],
                                            record["kernel_revision"])
            if selected != record["fifo_kernel_source_sha256"]:
                raise ValueError("compiled FIFO scheduler differs from frozen sources")
            kernel_proof = {
                "schema": "fifo-kernel-source-v1",
                "kernel_revision": record["kernel_revision"],
                "source_sha256": selected, "selected_source_matches": True,
                "source_root": str((firmware / "nuttx").resolve()),
                "expected_source_root": str((out / "firmware/nuttx").resolve()),
            }
            record["fifo_kernel_source_proof"] = "firmware/fifo-kernel-source.json"
            (out / record["fifo_kernel_source_proof"]).write_text(
                json.dumps(kernel_proof, indent=2) + "\n")
        image, elf, config = firmware / "nuttx" / profile["image"], firmware / "nuttx/nuttx", firmware / "resolved.config"
        for path in (image, elf, config):
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("missing or empty firmware artifact")
        boot_artifacts = []
        if boot_profile == boot_image.PROFILE:
            image, boot_fields, boot_artifacts = boot_image.prepare(firmware, image, out)
            record.update(boot_fields)
        record.update(
            resolved_configuration=baseline.validate_configuration(config, "baseline"),
            image_file=baseline.relative(image, out), image_sha256=sha(image),
            elf_file=baseline.relative(elf, out), elf_sha256=sha(elf),
            config_file=baseline.relative(config, out), config_sha256=sha(config),
            crossbeam_source_proof="firmware/crossbeam-source.json",
            std_source_proof="firmware/std-source.json", abi_receipt="firmware/abi-report.json",
        )
        artifact_paths = [image, elf, config,
                          firmware / "crossbeam-source.json", firmware / "std-source.json",
                          firmware / "std-patch.json", firmware / "abi-report.json", firmware / "provenance.txt",
                          out / "prepared/std-channel-hook.json", out / "prepared/std-library-sources.json",
                          out / "workspace/Cargo.lock", out / "workspace/Cargo.toml",
                          out / "crossbeam/crossbeam-hook.json", out / "crossbeam/cargo-config.toml"]
        artifact_paths += list((out / "crossbeam").glob("*.patch"))
        artifact_paths += list((out / "crossbeam/original").glob("*.crate"))
        artifact_paths += boot_artifacts
        if args.mode == "fifo-channel-suite":
            artifact_paths += [out / record["fifo_kernel_source_proof"]]
            artifact_paths += [out / "prepared/fifo-kernel" / name
                               for name in record["fifo_kernel_source_sha256"]]
        record["artifacts_sha256"] = {baseline.relative(p, out): sha(p) for p in artifact_paths}
        boot_image.verify(out, record)
        if inventory(profile) != before:
            raise ValueError("progress inputs changed during compilation")
        if any(sha(out / name) != digest for name, digest in staged_inputs.items()):
            raise ValueError("staged fixture changed during compilation")
        record.update(status="complete", failure=None)
    except Exception as error:
        record["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (out / "build.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=baseline.PLATFORMS, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-sysroot", type=Path)
    parser.add_argument("--crate-cache", type=Path, default=ROOT / "target/channel-progress-crates")
    parser.add_argument("--boot-image-profile", choices=(boot_image.DEFAULT, boot_image.PROFILE),
                        default=boot_image.DEFAULT,
                        help="opt-in paired ESP32-S3 image/loader; no physical qualification")
    args = parser.parse_args()
    try:
        record = build(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"progress image build failed: {error}\n")
    print("CHANNEL_PROGRESS_BUILD_COMPLETE", args.mode, args.platform, record["image_sha256"])


if __name__ == "__main__":
    main()
