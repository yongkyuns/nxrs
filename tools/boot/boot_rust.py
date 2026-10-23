"""Pinned no-std Rust build for the MCUboot policy callback; no device access."""
import json
import os
from pathlib import Path
import platform
import re
import shutil

import native_inputs
import qualify as process
import sign
import target_plan as policy

ROOT = policy.ROOT
SPEC = "security/esp32s3-boot-rust.json"
TARGETS = {"nxrs_boot_policy", "mcuboot_policy", "firmware_image", "core", "compiler_builtins"}


def trust_source(data, pin, public):
    from cryptography.hazmat.primitives import serialization
    settings = data["settings"]
    hashes = {"policy_sha256": pin, "configuration_sha256": settings["configuration_sha256"],
              "layout_sha256": sign.sha(sign.canonical(settings["layout"])),
              "key_sha256": sign.sha(public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.PKCS1))}
    result = "const TRUST: mcuboot_policy::firmware_image::Trust<'static> = mcuboot_policy::firmware_image::Trust {\n"
    result += "target: " + json.dumps(settings["target"]) + ",\n"
    for name, digest in hashes.items():
        policy.require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None)
        result += name + ":" + repr(list(bytes.fromhex(digest))) + ",\n"
    result += "layout: mcuboot_policy::firmware_image::Layout {" + ",".join(
        name + ":" + str(value) for name, value in settings["layout"].items()) + "}};\n"
    return result.encode("ascii")


def dependency_paths(path, *, base=None):
    text = native_inputs.bounded(path).decode()
    line = text.splitlines()[0]
    policy.require(": " in line and "\\" not in line)
    paths = line.split(": ", 1)[1].split()
    policy.require(0 < len(paths) <= 4096 and len(set(paths)) == len(paths))
    base = Path.cwd() if base is None else base
    result = [(Path(name) if Path(name).is_absolute() else base / name).resolve(strict=True) for name in paths]
    policy.require(len(set(result)) == len(result))
    return result


def install(work, cache, environment):
    from build_esp32s3 import archive
    spec = json.loads((ROOT / SPEC).read_bytes())
    host = platform.system() + "-" + platform.machine()
    policy.require(spec["schema"] == 1 and host in spec["hosts"]
                   and spec["version"] == "1.90.0.0" and spec["cargo_version"] == "1.98.1")
    folder = work / "rust"
    folder.mkdir(mode=0o700)
    compiler = archive(spec["hosts"][host], folder, "compiler", cache, 256 * 1024 * 1024)
    source = archive(spec["source"], folder, "source", cache, 32 * 1024 * 1024)
    runtime = folder / "runtime"
    status, _ = process.run(["bash", str(compiler / "install.sh"), "--prefix=" + str(runtime), "--disable-ldconfig"],
                           work, environment, work / "logs", "rust-install", 180)
    policy.require(status == 0)
    manifests = list(source.glob("**/library/core/Cargo.toml"))
    policy.require(len(manifests) == 1)
    library = manifests[0].parent.parent
    destination = runtime / "lib/rustlib/src/rust"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "library").symlink_to(library)
    rustc = runtime / "bin/rustc"
    status, version = process.run([str(rustc), "-vV"], work, environment, work / "logs", "rust-version", 30)
    policy.require(status == 0 and "commit-hash: " + spec["commit"] + "\n" in version
                   and "LLVM version: " + spec["llvm"] + "\n" in version
                   and "(1.90.0.0)\n" in version.split("binary:")[0])
    return spec, spec["hosts"][host], runtime, library, version


def observe(messages, inputs, target_dir, library):
    artifacts, scripts = {}, []
    entries = {"nxrs_boot_policy": ROOT / "platform/mcuboot/src/lib.rs",
               "mcuboot_policy": ROOT / "driver/mcuboot-policy/src/lib.rs",
               "firmware_image": ROOT / "driver/firmware-image/src/lib.rs",
               "core": library / "core/src/lib.rs",
               "compiler_builtins": library / "compiler-builtins/compiler-builtins/src/lib.rs"}
    final = None
    for line in messages.splitlines():
        item = json.loads(line)
        if item.get("reason") != "compiler-artifact":
            continue
        target = item["target"]
        if target["kind"] == ["custom-build"]:
            path = Path(target["src_path"]).resolve(strict=True)
            policy.require(path in {ROOT / "platform/mcuboot/build.rs", library / "compiler-builtins/compiler-builtins/build.rs"})
            binaries = [Path(name).resolve(strict=True) for name in item["filenames"]]
            policy.require(len(binaries) == 1 and binaries[0].is_relative_to(target_dir)
                           and item["package_id"].startswith("path+file://"))
            dependencies = list(binaries[0].parent.glob("*.d"))
            policy.require(len(dependencies) == 1)
            base = ROOT if path.is_relative_to(ROOT) else library
            sources = sorted(inputs.add(name) for name in dependency_paths(dependencies[0], base=base))
            entry_id = inputs.add(path)
            policy.require(entry_id in sources)
            scripts.append({"entry": entry_id, "sources": sources,
                            "artifact_sha256": process.digest(binaries[0]), "artifact_bytes": binaries[0].stat().st_size})
            continue
        name = target["name"]
        policy.require(name in TARGETS and name not in artifacts
                       and item["package_id"].startswith("path+file://")
                       and Path(target["src_path"]).resolve(strict=True) == entries[name])
        paths = [Path(value).resolve(strict=True) for value in item["filenames"]]
        policy.require(all(path.is_relative_to(target_dir) for path in paths))
        native = [path for path in paths if path.suffix in (".a", ".rlib")]
        policy.require(len(native) == 1)
        archive_path = native[0]
        dep = archive_path.with_suffix(".d") if name == "nxrs_boot_policy" else archive_path.with_name(archive_path.name[3:-5] + ".d")
        source_paths = dependency_paths(dep, base=ROOT if name in {"nxrs_boot_policy", "mcuboot_policy", "firmware_image"} else library)
        source_ids = sorted(inputs.add(path) for path in source_paths)
        entry = Path(target["src_path"]).resolve(strict=True)
        policy.require(inputs.add(entry) in source_ids)
        version = item["package_id"].split("#")[-1].split("@")[-1]
        policy.require(re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version) is not None)
        artifacts[name] = {"name": name, "version": version, "sources": source_ids, "artifact_sha256": process.digest(archive_path),
                           "artifact_bytes": archive_path.stat().st_size}
        if name == "nxrs_boot_policy":
            final = archive_path
    policy.require(set(artifacts) == TARGETS and len(scripts) == 2)
    return artifacts, scripts, final



def build(work, data, pin, public, compiler, environment, cache):
    work = work.resolve(strict=True)
    spec, archive, runtime, library, version = install(work, cache, environment)
    inputs = native_inputs.Inputs(work, next((work / "mcuboot-source").iterdir()),
                                 next((work / "hal-source").iterdir()), compiler)
    output = work / "build/boot-policy"
    output.mkdir(parents=True, mode=0o700)
    trust = output / "trust.rs"
    trust.write_bytes(trust_source(data, pin, public))
    policy.require(trust.stat().st_size <= 8192)
    rustc = runtime / "bin/rustc"
    tool_env = dict(environment, PATH=os.environ["PATH"],
                    RUSTUP_HOME=os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")))
    status, actual = process.run(["rustup", "which", "--toolchain", "1.98.1", "cargo"], work,
                                tool_env, work / "logs", "rust-cargo-path", 30)
    policy.require(status == 0)
    actual = Path(actual.strip()).resolve(strict=True)
    status, cargo_path = process.run([str(actual), "--version"], work,
                                    tool_env, work / "logs", "rust-cargo-version", 30)
    policy.require(status == 0 and re.fullmatch(r"cargo 1[.]98[.]1 [(][0-9a-f]+ [0-9-]+[)]\n", cargo_path))
    target_dir = work / "rust/target"
    flags = "--remap-path-prefix=" + str(ROOT) + "=/nxrs --remap-path-prefix=" + str(work) + "=/nxrs-build"
    env = dict(environment, RUSTC=str(rustc), RUSTC_BOOTSTRAP="1", CARGO_TARGET_DIR=str(target_dir),
        CARGO_HOME=str(work / "rust/cargo"), CARGO_INCREMENTAL="0", CARGO_BUILD_JOBS="2",
        CARGO_PROFILE_RELEASE_LTO="fat", CARGO_PROFILE_RELEASE_CODEGEN_UNITS="1", CARGO_PROFILE_RELEASE_OPT_LEVEL="z",
        CARGO_PROFILE_RELEASE_DEBUG="1", CARGO_PROFILE_RELEASE_STRIP="none", CARGO_PROFILE_RELEASE_PANIC="abort",
        NXRS_BOOT_TRUST_RUST=str(trust), RUSTFLAGS=flags)
    # RUSTUP_HOME is used only to locate separately pinned upstream Cargo. No
    # global toolchain switch, target installation, or user Cargo configuration.
    env["RUSTUP_HOME"] = tool_env["RUSTUP_HOME"]
    env["PATH"] = os.environ["PATH"]
    command = [str(actual), "rustc", "--manifest-path", str(ROOT / "Cargo.toml"), "--locked", "-p", "nxrs-boot-policy", "--lib", "--crate-type", "staticlib",
               "--target", spec["target"], "-Z", "build-std=core,compiler_builtins", "--release", "--message-format=json"]
    status, messages = process.run(command, work, env, work / "logs", "rust-build", 300)
    policy.require(status == 0)
    artifacts, scripts, built = observe(messages, inputs, target_dir, library)
    final = output / "libnxrs_boot_policy.a"
    shutil.copyfile(built, final)
    archive_id = inputs.add(final)
    for name in (SPEC, "tools/boot/boot_rust.py", "platform/mcuboot/Cargo.toml", "platform/mcuboot/build.rs",
                 "driver/mcuboot-policy/Cargo.toml", "driver/firmware-image/Cargo.toml", "Cargo.toml", "Cargo.lock"):
        inputs.add(ROOT / name)
    inputs.add(library / "Cargo.lock")
    for sub in ("core", "compiler-builtins/compiler-builtins"):
        inputs.add(library / sub / "Cargo.toml")
    status, target = process.run([str(rustc), "-Z", "unstable-options", "--print", "target-spec-json", "--target", spec["target"]],
                                work, env, work / "logs", "rust-target", 30)
    target = json.loads(target)
    policy.require(status == 0 and target["arch"] == "xtensa" and target["cpu"] == "esp32s3"
                   and target["target-pointer-width"] == "32" and target["max-atomic-width"] == 32
                   and target["panic-strategy"] == "abort" and target["metadata"]["std"] is False)
    return {"schema": 1, "scope": "actual-no-std-xtensa-policy-build", "archive": archive_id,
        "archive_sha256": process.digest(final), "archive_bytes": final.stat().st_size,
        "compiler_archive": archive, "library_archive": spec["source"], "compiler": version.strip(),
        "rustc_sha256": process.digest(rustc), "cargo": cargo_path.strip(), "cargo_sha256": process.digest(actual),
        "target": target, "command_sha256": policy.digest(policy.canonical(["cargo-1.98.1", *[inputs.normalized(value) for value in command[1:]]])),
        "profile": {"lto": "fat", "codegen_units": 1, "opt_level": "z", "panic": "abort", "incremental": False},
        "trust_source_sha256": process.digest(trust), "components": [artifacts[name] for name in sorted(artifacts)],
        "host_build_scripts": sorted(scripts, key=lambda item: item["entry"]), "files": sorted(inputs.files.values(), key=lambda item: item["id"]),
        "registry_code_compiled": False, "rust_advisory_closure": False, "boot_stack_qualified": False,
        "watchdog_progress_wired": True, "watchdog_qualified": False, "physical_qualified": False,
        "target_hook_executed": False}


def verify(report, inputs):
    policy.require(report["schema"] == 1 and {c["name"] for c in report["components"]} == TARGETS
                   and report["registry_code_compiled"] is False and report["rust_advisory_closure"] is False
                   and all(report[name] is False for name in ("boot_stack_qualified", "watchdog_qualified", "physical_qualified", "target_hook_executed")))
    policy.require(report["profile"] == {"lto": "fat", "codegen_units": 1, "opt_level": "z", "panic": "abort", "incremental": False})
    for file in report["files"]:
        prefix, relative = file["id"].split(":", 1)
        policy.require(prefix in inputs.roots)
        path = inputs.roots[prefix] / relative
        policy.require(inputs.add(path) == file["id"] and inputs.files[file["id"]] == file)
    spec = json.loads((ROOT / SPEC).read_bytes())
    policy.require(report["compiler_archive"] in spec["hosts"].values() and report["library_archive"] == spec["source"]
                   and "commit-hash: " + spec["commit"] in report["compiler"])
    process.verify(inputs.work / "rust/compiler.archive", report["compiler_archive"]["sha256"])
    process.verify(inputs.work / "rust/source.archive", spec["source"]["sha256"])
    manifests = list((inputs.work / "rust/source-source").glob("**/library/core/Cargo.toml"))
    policy.require(len(manifests) == 1)
    artifacts, scripts, built = observe((inputs.work / "logs/rust-build.out").read_text(), inputs,
                                       inputs.work / "rust/target", manifests[0].parent.parent)
    policy.require([artifacts[name] for name in sorted(artifacts)] == report["components"]
                   and sorted(scripts, key=lambda item: item["entry"]) == report["host_build_scripts"]
                   and process.digest(built) == report["archive_sha256"])
    archive = inputs.files[report["archive"]]
    policy.require(archive["sha256"] == report["archive_sha256"] and archive["bytes"] == report["archive_bytes"])
