#!/usr/bin/env python3
"""Check the finite two-profile concurrent CoreS3 build witness.

This checks preparation isolation and the completed target builds. It does not
infer concurrent CPU execution, build speedup, static object sizes or hardware
behavior. Replay can relocate the source checkout and the two sibling outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

CASES = ("bmi270", "lsm6dso")
TARGET = "xtensa-esp32s3-nuttx"
PROFILE = "m5stack-cores3-gnss"
OVERLAY = "tests/host/fixtures/lsm6dso-overlay.toml"
BSP = "platform/nuttx/resources/esp32s3-m5-cores3.toml"
SOURCE_INPUTS = (
    "Cargo.toml", "Cargo.lock", "tools/qualify-concurrent-builds.py",
    "tools/check-concurrent-builds.py", ".github/workflows/event-demo-self-hosted.yml",
    *(f"tools/build/src/{name}.rs" for name in
      ("main", "build", "nuttx", "cargo_graph", "deployment", "sdk", "source")),
    *(f"tools/build-support/src/{name}.rs" for name in
      ("preparation", "resolved", "configuration", "prepared_resource")),
    "platform/cores3/profile.toml", BSP, OVERLAY,
)
NATIVE = {"nxrs-acquisition": "hal/acquisition", "nxrs-gnss": "hal/gnss",
          "nxrs-gpio": "driver/gpio", "nxrs-i2c": "driver/i2c"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_file(path: Path):
    return json.loads(path.read_text())


def integer(value, label: str) -> int:
    require(type(value) is int and value >= 0, f"invalid {label}")
    return value


def sha(value) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def absolute(value, label: str) -> Path:
    require(isinstance(value, str), f"invalid {label} path")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, f"noncanonical {label} path")
    return path


def relative(value: str) -> Path:
    require(isinstance(value, str), "invalid evidence-relative path")
    path = Path(value)
    require(not path.is_absolute()
            and value not in ("", ".") and ".." not in path.parts,
            "invalid evidence-relative path")
    return path


def input_digest(path: Path) -> str:
    """The native preparer's ordered file/directory digest, without executing it."""
    if path.is_file():
        return digest(path)
    require(path.is_dir() and not path.is_symlink(), "missing native source input")
    entries = {}
    for entry in path.rglob("*"):
        require(not entry.is_symlink(), "native source tree contains a symlink")
        entries[entry.relative_to(path).as_posix()] = "directory" if entry.is_dir() else digest(entry)
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Context:
    def __init__(self, original_source: Path, source: Path,
                 original_out: Path, out: Path):
        self.original_source = original_source
        self.source = source.resolve()
        self.original_out = original_out
        self.out = out.resolve()

    def output(self, value, prefix="") -> Path:
        path = absolute(value, "output")
        require(path.is_relative_to(self.original_out / prefix),
                f"mutable output escapes its own root: {path}")
        return self.out / path.relative_to(self.original_out)

    def source_file(self, value, *, directory=False) -> Path:
        path = absolute(value, "source")
        require(path.is_relative_to(self.original_source), "source escapes checkout")
        rel = path.relative_to(self.original_source)
        require(rel.parts and rel.parts[0] not in ("target", "external", ".git"),
                "generated path presented as a source input")
        result = self.source / rel
        require((result.is_dir() if directory else result.is_file())
                and result.resolve().is_relative_to(self.source),
                f"missing/outside source input: {rel}")
        return result

    def evidence(self, path: Path, hashes: dict[str, str]) -> None:
        require(path.is_relative_to(self.out), "evidence outside case output")
        require(path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(self.out), f"missing/escaped evidence: {path}")
        hashes[path.relative_to(self.out).as_posix()] = digest(path)


def expected_graph(case: str) -> dict:
    require(case in CASES, "unknown concurrent-build case")
    alternate = case == "lsm6dso"
    resources = {"receiver_uart": {"kind": "uart", "device": "/dev/ttyS1", "baud": 38400},
                 "sensor_bus": {"kind": "i2c", "device": "/dev/i2c0", "frequency": 400000}}
    if alternate:
        resources["alternate_i2c"] = {"kind": "i2c", "device": "/dev/i2c1", "frequency": 100000}
    return {
        "version": 1, "mode": "hardware", "resources": resources,
        "devices": {
            "imu": {"compatible": "st,lsm6dso" if alternate else "bosch,bmi270",
                    "bus": "alternate_i2c" if alternate else "sensor_bus",
                    "address": 106 if alternate else 104, "wake": {"kind": "periodic"}},
            "navigation": {"compatible": "u-blox,neo-m9n", "bus": "receiver_uart"},
        },
        "aliases": {"imu": "imu", "gnss": "navigation"},
        "imu_implementation": "Lsm6dso" if alternate else "Bmi270",
        "gnss_implementation": "NeoM9n", "stack_bytes": 16384, "gnss_stack_bytes": 16384,
    }


def expected_generated(case: str, capability: str) -> str:
    if capability == "imu":
        if case == "bmi270":
            driver, device, frequency, address = (
                "::r#bmi270::Bmi270<resources::Bus, resources::Delay>", "/dev/i2c0", 400000, 104)
        else:
            driver, device, frequency, address = (
                "::r#lsm6dso::Lsm6dso<resources::Bus>", "/dev/i2c1", 100000, 106)
        arguments = (f'resources::I2cResource, resources::open_i2c, '
                     f'(resource::Config {{ path: "{device}", frequency: {frequency} }}, {address}), None')
        identifier, reference, accessors = "imu", "true", ("imu", "primary_imu")
    else:
        driver, arguments = '::r#ubx::NeoM9n<Serial>', '"/dev/ttyS1", 38400'
        identifier, reference, accessors = "navigation", "false", ("navigation", "primary_gnss")
    return ("// Generated canonical device instances.\n"
            f'declare_device!(PRIMARY_DEVICE, "{identifier}", {driver}, {arguments}, 16384, {reference});\n'
            "pub mod devices {\n" + "".join(
                f"pub fn r#{name}() -> &'static impl super::Device {{ &super::PRIMARY_DEVICE }}\n"
                for name in accessors) + "}\n")


def _prepared(case: str, ctx: Context) -> tuple[dict[str, str], dict]:
    receipt = json_file(ctx.out / "resolved-profile.json")
    selected = json_file(ctx.out / "provider-selection.json")
    hashes: dict[str, str] = {}
    for name in (".nxrs-build.json", "resolved-profile.json", "provider-selection.json"):
        ctx.evidence(ctx.out / name, hashes)
    require(json_file(ctx.out / ".nxrs-build.json") == {"workspace": str(ctx.original_source)},
            "build output is owned by another checkout")
    require(receipt["version"] == 1 and receipt["workspace"] == str(ctx.original_source)
            and receipt["profile"] == PROFILE and receipt["target"] == TARGET
            and receipt["os"] == "nuttx", "wrong resolved profile context")
    overlays = [str(ctx.original_source / OVERLAY)] if case == "lsm6dso" else []
    require(receipt["cli_overlays"] == ([OVERLAY] if case == "lsm6dso" else []), "wrong ordered overlays")
    require(receipt.get("product_manifest") == str(ctx.original_source / "app/event-demo/Cargo.toml"),
            "wrong product manifest context")
    graph = expected_graph(case)
    require(receipt["graph"] == graph, "wrong independent composition contract")
    require(receipt["data"]["target"] == TARGET
            and receipt["data"]["board"] == "esp32s3-m5-cores3:nsh",
            "wrong target or board")
    semantic = {"graph": graph, "target": TARGET,
                "nuttx-resources": receipt["data"].get("resolved-nuttx-resources")}
    semantic_digest = hashlib.sha256(json.dumps(semantic, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    require(receipt["semantic_digest"] == semantic_digest, "incorrect semantic digest")
    require(sha(receipt["raw_fingerprint"]), "missing resolved input fingerprint")
    dependencies = receipt["dependencies"]
    require(isinstance(dependencies, list) and dependencies, "missing source dependencies")
    seen = set()
    for item in dependencies:
        source = ctx.source_file(item["path"])
        require(item["path"] not in seen and sha(item["sha256"])
                and digest(source) == item["sha256"], "changed/duplicate profile source")
        seen.add(item["path"])
    require(str(ctx.original_source / "platform/cores3/profile.toml") in seen
            and all(path in seen for path in overlays), "missing profile/overlay source")
    require(selected["app_manifest"] == str(ctx.original_source / "app/event-demo/Cargo.toml")
            and selected["app_package"] == "nxrs-event-demo"
            and selected["platform"] == PROFILE and selected["target_os"] == "nuttx"
            and selected["nuttx-build-mode"] == "flat", "wrong selected deployment")
    composition = selected["resolved-composition"]
    require(composition["graph"] == graph
            and composition["semantic-digest"] == semantic_digest
            and composition["input-fingerprint"] == receipt["raw_fingerprint"],
            "selection and profile receipt disagree")
    sources = [str(ctx.original_source / "platform/cores3/profile.toml"), *overlays,
               str(ctx.original_source / BSP)]
    require(all(path in seen for path in sources), "missing profile/BSP source dependency")
    require(composition["sources"] == sources, "wrong selected profile source order")
    require(selected["profiles"] == [{"path": path, "sha256": digest(ctx.source_file(path))}
                                      for path in sources], "wrong selected profile hashes")
    require(selected["dependency_resolution"]["status"] == "resolved", "unresolved Cargo graph")
    production = {value.split()[0] for value in selected["dependency_resolution"]["packages"]}
    wanted, excluded = ("nxrs-bmi270", "nxrs-lsm6dso") if case == "bmi270" else ("nxrs-lsm6dso", "nxrs-bmi270")
    require({"nxrs-event-demo", "nxrs-composition", "nxrs-imu", "nxrs-gnss", "nxrs-ubx-driver", wanted}
            <= production and excluded not in production, "wrong production driver selection")
    handoff = ctx.output(selected["configuration"], "configuration")
    ctx.evidence(handoff, hashes)
    require(handoff.name == hashes[handoff.relative_to(ctx.out).as_posix()] + ".json",
            "prepared handoff identity mismatch")
    prepared = json_file(handoff)
    require(set(prepared) == {str(ctx.original_source / "platform")}, "wrong prepared catalogue")
    capabilities = prepared[str(ctx.original_source / "platform")]
    require(set(capabilities) == {"imu", "gnss"}, "wrong prepared capabilities")
    for capability, record in capabilities.items():
        require(sha(record["identity"]) and record["output"]["file"] == f"{capability}_config.rs",
                "invalid generated configuration identity")
        generated = handoff.parent / record["identity"] / record["output"]["file"]
        ctx.evidence(generated, hashes)
        require(digest(generated) == record["output"]["sha256"]
                and generated.read_text() == expected_generated(case, capability),
                "incorrect generated device construction")
    context_path = ctx.out / "native-context.json"
    ctx.evidence(context_path, hashes)
    native = json_file(context_path)
    require(native["schema"] == 1 and native["target"] == TARGET
            and native["directory"] == str(ctx.original_out / "apps/examples/nxrs_std_app")
            and native["include"] == str(ctx.original_out / "nuttx/include"),
            "native context belongs to another build")
    tools = []
    for key in ("cc", "ar"):
        require(isinstance(native[key], list) and native[key], "missing native tool")
        tool = absolute(native[key][0], "native tool")
        require(tool.name == f"xtensa-esp32s3-elf-{'gcc' if key == 'cc' else 'ar'}"
                and not tool.is_relative_to(ctx.original_out.parent),
                "native tool is not the shared immutable Xtensa tool")
        tools.append(tool)
    require(tools[0].parent == tools[1].parent and tools[0].parent.name == "bin",
            "native tools belong to different installations")
    tool_root = tools[0].parent.parent
    require(isinstance(native["cflags"], list), "missing native C flags")
    flags = iter(native["cflags"])
    for flag in flags:
        value = None
        if flag in ("-I", "-isystem", "-iquote", "-include"):
            value = next(flags, None)
            require(value is not None, "missing native include argument")
        elif flag.startswith("-I"):
            value = flag[2:]
        if value:
            path = Path(value)
            if not path.is_absolute():
                path = ctx.original_out / "apps/examples/nxrs_std_app" / path
            require(".." not in path.parts and (path.is_relative_to(ctx.original_out)
                    or path.is_relative_to(tool_root)), "native C include escapes its build")
    require(sha(native["config_sha256"]) and sha(native["headers_sha256"]), "missing native header identity")
    archives = selected["native-resources"]
    require(isinstance(archives, list) and len(archives) == len(NATIVE), "wrong native resource count")
    owners = set()
    for resource in archives:
        owner = resource["package"].split()[0]
        require(owner in NATIVE and owner not in owners, "wrong/duplicate native owner")
        owners.add(owner)
        archive = ctx.output(resource["archive"], "native-resources")
        ctx.evidence(archive, hashes)
        owner_root = ctx.original_source / NATIVE[owner]
        bucket = hashlib.sha256(json.dumps(str(owner_root), ensure_ascii=False).encode()).hexdigest()
        require(archive == ctx.out / "native-resources" / bucket / resource["sha256"]
                / f"lib{owner.removeprefix('nxrs-')}_native.a", "wrong native archive identity path")
        require(digest(archive) == resource["sha256"]
                and archive.read_bytes().startswith(b"!<arch>\n"), "native archive hash/type mismatch")
        inputs = archive.parent.parent / "inputs.json"
        ctx.evidence(inputs, hashes)
        record = json_file(inputs)
        require(sha(record["identity"]) and sha(record["tool_path"])
                and record["archive"] == resource["sha256"]
                and record["inputs"].get(str(ctx.original_out / "native-context.json")) == digest(context_path),
                "native archive consumed another context")
        require(str(owner_root / "Cargo.toml") in record["inputs"]
                and str(owner_root / "src/platform/nuttx/ffi") in record["inputs"],
                "wrong native resource source owner")
        external = []
        for name, checksum in record["inputs"].items():
            require(sha(checksum), "invalid native input digest")
            path = absolute(name, "native input")
            if path.is_relative_to(ctx.original_out):
                local = ctx.output(name)
                ctx.evidence(local, hashes)
                require(digest(local) == checksum, "changed prepared native input")
            elif path.is_relative_to(owner_root):
                local = ctx.source_file(name, directory=path == owner_root / "src/platform/nuttx/ffi")
                require(input_digest(local) == checksum, "native source input changed")
            else:
                # Tool binaries and their bundled headers are immutable inputs.
                # Their recorded hashes are retained; replay needs no SDK install.
                require(path.is_relative_to(tool_root), "native input escapes its build/source/tool roots")
                external.append(path)
        require(any(path.name.endswith("-gcc") for path in external)
                and any(path.name.endswith("-ar") for path in external), "native tool hashes missing")
    packages = ctx.out / "native-resources/packages.json"
    ctx.evidence(packages, hashes)
    require(json_file(packages) == sorted(str(ctx.original_source / path) for path in NATIVE.values()),
            "wrong prepared native package list")
    return dict(sorted(hashes.items())), {"semantic_digest": semantic_digest,
        "input_fingerprint": receipt["raw_fingerprint"], "production_packages": sorted(production)}


def snapshot_prepared(case: str, out: Path, source_root: Path) -> dict[str, str] | None:
    """Return no snapshot until the final complete provider selection is readable."""
    try:
        selected = json_file(out / "provider-selection.json")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if (not selected.get("configuration")
            or selected.get("dependency_resolution", {}).get("status") != "resolved"
            or "native-resources" not in selected):
        return None
    return _prepared(case, Context(source_root.resolve(), source_root, out.resolve(), out))[0]


def package_name(message: dict) -> str:
    identity = message["package_id"]
    fragment = identity.rsplit("#", 1)[-1]
    if "@" in fragment:
        name = fragment.split("@", 1)[0]
    else:
        # Cargo omits a redundant name for path packages such as build-std's
        # `.../library/std#0.0.0`.
        require(identity.startswith("path+file://") and re.fullmatch(r"[0-9]+\..+", fragment),
                "unsupported Cargo package identity")
        name = Path(unquote(urlsplit(identity.removeprefix("path+")).path)).name
    require(re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None, "invalid Cargo package name")
    return name


def check_elf(path: Path, expected_type: int) -> None:
    with path.open("rb") as stream:
        header = stream.read(20)
    require(len(header) == 20 and header[:7] == b"\x7fELF\x01\x01\x01"
            and struct.unpack_from("<HH", header, 16) == (expected_type, 94),
            f"wrong ELF class/machine/type: {path}")


def check_native_configuration(case: str, out: Path) -> None:
    """Bind the fixed Rust endpoints to the actual NuttX bus registrations."""
    expected = {"I2C": "y", "I2C_DRIVER": "y", "ESP32S3_I2C0": "y",
                "ESP32S3_I2C0_MASTER_MODE": "y", "ESP32S3_I2C0_SCLPIN": "11",
                "ESP32S3_I2C0_SDAPIN": "12", "ESP32S3_UART1": "y",
                "ESP32S3_UART1_TXPIN": "17", "ESP32S3_UART1_RXPIN": "18", "UART1_BAUD": "38400",
                "ESP32S3_I2C1": "y" if case == "lsm6dso" else None}
    if case == "lsm6dso":
        expected.update(ESP32S3_I2C1_MASTER_MODE="y", ESP32S3_I2C1_SCLPIN="39", ESP32S3_I2C1_SDAPIN="38")
    expected = {"CONFIG_" + key: value for key, value in expected.items()}
    config, header = {}, {}
    for line in (out / "resolved.config").read_text().splitlines():
        setting = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        match = setting or disabled
        if match and match[1] in expected:
            require(match[1] not in config, "duplicate native Kconfig setting")
            config[match[1]] = setting[2] if setting else None
    for line in (out / "nuttx/include/nuttx/config.h").read_text().splitlines():
        directive = re.fullmatch(r"\s*#\s*(define|undef)\s+(CONFIG_[A-Z0-9_]+)(?:\s+(.*?))?\s*", line)
        if directive and directive[2] in expected:
            require(directive[2] not in header, "duplicate native configuration directive")
            header[directive[2]] = (directive[3] or "") if directive[1] == "define" else None
    require(config == expected, f"native Kconfig does not implement the {case} device contract")
    # NuttX emits no macro for its normal not-set comments, and #undef for
    # explicit '=n'. Either is disabled; '#define ... 0' is still a definition.
    for key, value in expected.items():
        require(header.get(key) == ("1" if value == "y" else value),
                f"native header does not implement the {case} device contract: {key}")


def _finished(case: str, ctx: Context, revision: str) -> tuple[dict[str, str], dict]:
    hashes, summary = _prepared(case, ctx)
    required = ("cargo-messages.jsonl", "std-source.json", "rust-link.json", "provenance.txt",
                "binaries.sha256", "downloads.sha256", "std-patch.json", "resolved.config",
                "nuttx-patches.json", "nuttx-apps-patches.json", "esp-hal-patches.json",
                "rust-input.elf", "rust-partial.map", "nxrs-nuttx-link", "nuttx/nuttx", "nuttx/nuttx.bin",
                f"cargo/{TARGET}/release/event-demo", "nuttx/include/nuttx/config.h")
    for name in required:
        ctx.evidence(ctx.out / name, hashes)
    patchset = json_file(ctx.out / "std-patch.json")
    require(patchset["sdk"] == "esp-1.90.0.0" and patchset["installed_toolchain_unchanged"] is True
            and isinstance(patchset["patchset_sha256"], dict) and patchset["patchset_sha256"],
            "missing private SDK patch provenance")
    for name, checksum in patchset["patchset_sha256"].items():
        require(relative(name).name == name and name.endswith(".patch") and sha(checksum),
                "invalid retained SDK patch")
        patch = ctx.out / name
        ctx.evidence(patch, hashes)
        require(digest(patch) == checksum, "SDK patch bytes changed")
    native = json_file(ctx.out / "native-context.json")
    require(digest(ctx.out / "nuttx/include/nuttx/config.h") == native["config_sha256"],
            "native config header changed")
    check_native_configuration(case, ctx.out)
    provenance = (ctx.out / "provenance.txt").read_text().splitlines()
    for expected in (f"nxrs={revision}", "nxrs_dirty=no", "app_package=nxrs-event-demo",
                     "app_bin=event-demo", f"platform={PROFILE}", f"target={TARGET}",
                     "nuttx_build_mode=flat", "board=esp32s3-m5-cores3:nsh"):
        require(provenance.count(expected) == 1, f"wrong/missing provenance: {expected}")
    messages = [json.loads(line) for line in (ctx.out / "cargo-messages.jsonl").read_text().splitlines()]
    require(sum(row.get("reason") == "build-finished" and row.get("success") is True
                for row in messages) == 1, "missing successful target Cargo completion")
    require(not any(row.get("reason") == "build-finished" and row.get("success") is not True
                    for row in messages), "failed target Cargo completion")
    target_packages = set()
    selected_libraries = {"nxrs-" + case: "driver/" + case, "nxrs-composition": "platform",
                          "nxrs-imu": "hal/imu", "nxrs-gnss": "hal/gnss", "nxrs-ubx-driver": "driver/ubx"}
    retained_libraries = set()
    consumed = {}
    consumed_native = set()
    root_artifacts = []
    selected_native = {row["package"].split()[0]: absolute(row["archive"], "native archive")
                       for row in json_file(ctx.out / "provider-selection.json")["native-resources"]}
    for row in messages:
        if row.get("reason") == "compiler-artifact":
            for filename in row["filenames"]:
                ctx.output(filename, "cargo")
            if any(absolute(filename, "artifact").is_relative_to(ctx.original_out / "cargo" / TARGET)
                   for filename in row["filenames"]):
                package = package_name(row)
                target_packages.add(package)
                if package in selected_libraries:
                    require(package not in retained_libraries and row["fresh"] is False
                            and row["target"]["kind"] == ["lib"]
                            and row["target"]["src_path"] == str(ctx.original_source / selected_libraries[package] / "src/lib.rs"),
                            "wrong selected target library artifact")
                    archives = [ctx.output(name, f"cargo/{TARGET}/release/deps")
                                for name in row["filenames"] if name.endswith(".rlib")]
                    require(len(archives) == 1, "missing selected target rlib")
                    ctx.evidence(archives[0], hashes)
                    with archives[0].open("rb") as archive:
                        require(archive.read(8) == b"!<arch>\n", "invalid selected target rlib")
                    retained_libraries.add(package)
            if row.get("executable") is not None:
                ctx.output(row["executable"], "cargo")
                if package_name(row) == "nxrs-event-demo":
                    root_artifacts.append(row)
        elif row.get("reason") == "build-script-executed":
            directory = ctx.output(row["out_dir"], "cargo")
            for path in row.get("linked_paths", []):
                ctx.output(path.split("=", 1)[-1])
            name = package_name(row)
            if name == "nxrs-composition":
                require(absolute(row["out_dir"], "composition OUT_DIR").is_relative_to(
                    ctx.original_out / "cargo" / TARGET), "configuration consumed by host build")
                for capability in ("imu", "gnss"):
                    path = directory / f"{capability}_config.rs"
                    ctx.evidence(path, hashes)
                    require(capability not in consumed and path.read_text() == expected_generated(case, capability),
                            "wrong/duplicate consumed configuration")
                    receipt = directory / f"nxrs-composition-{capability}.configuration.json"
                    ctx.evidence(receipt, hashes)
                    require(json_file(receipt) == {"version": 1, "file": path.name},
                            "incorrect Cargo configuration receipt")
                    consumed[capability] = path.relative_to(ctx.out).as_posix()
            if name in NATIVE:
                require(name not in consumed_native
                        and f"native={selected_native[name].parent}" in row["linked_paths"]
                        and f"static={name.removeprefix('nxrs-').replace('-', '_')}_native" in row["linked_libs"],
                        "Cargo consumed another prepared native archive")
                consumed_native.add(name)
    require(set(consumed) == {"imu", "gnss"}, "missing consumed configuration evidence")
    require(retained_libraries == set(selected_libraries), "missing selected target library artifacts")
    require(consumed_native == set(NATIVE), "missing consumed native resource evidence")
    require(len(root_artifacts) == 1 and root_artifacts[0]["fresh"] is False
            and root_artifacts[0]["executable"] == str(ctx.original_out / "cargo" / TARGET / "release/event-demo")
            and root_artifacts[0]["target"]["kind"] == ["bin"]
            and root_artifacts[0]["target"]["src_path"] == str(ctx.original_source / "app/event-demo/src/main.rs"),
            "missing fresh source-bound event-demo target artifact")
    wanted, excluded = ("nxrs-bmi270", "nxrs-lsm6dso") if case == "bmi270" else ("nxrs-lsm6dso", "nxrs-bmi270")
    require({wanted, "nxrs-composition", "nxrs-event-demo", "nxrs-imu", "nxrs-gnss", "nxrs-ubx-driver"}
            <= target_packages and excluded not in target_packages, "wrong actual target driver artifacts")
    std = json_file(ctx.out / "std-source.json")
    expected_std = str(ctx.original_out / "toolchain/lib/rustlib/src/rust/library/std/src/lib.rs")
    require(std["selected_source_matches"] is True and std["std_source"] == expected_std
            and std["expected_source"] == expected_std
            and std["rustc"] == str(ctx.original_out / "toolchain/bin/rustc"),
            "shared or incorrect private Rust SDK")
    link = json_file(ctx.out / "rust-link.json")
    outputs = [args[index + 1] for args in (link["original"], link["effective"])
               for index, item in enumerate(args[:-1]) if item == "-o"]
    require(len(outputs) == 2 and outputs[0] == outputs[1], "ambiguous Rust link output")
    ctx.output(outputs[0], f"cargo/{TARGET}/release")
    require(f"-Map={ctx.original_out / 'rust-partial.map'}" in link["effective"]
            and all(flag in link["effective"] for flag in ("-r", "--gc-sections", "-u", "main")),
            "wrong relocatable link evidence")
    require(digest(ctx.out / "cargo" / TARGET / "release/event-demo") == digest(ctx.out / "rust-input.elf"),
            "retained Rust input differs from linked Cargo ELF")
    check_elf(ctx.out / "rust-input.elf", 1)
    check_elf(ctx.out / "nuttx/nuttx", 2)
    binary_paths = {}
    for line in (ctx.out / "binaries.sha256").read_text().splitlines():
        checksum, recorded = line.split("  ", 1)
        require(sha(checksum) and binary_paths.get(recorded, checksum) == checksum,
                "invalid/conflicting binary digest")
        # The normal frontend lists catalogue inputs once per consumed
        # capability; repeated identical source hashes are legitimate.
        binary_paths[recorded] = checksum
        path = absolute(recorded, "binary digest")
        if path.is_relative_to(ctx.original_out):
            current = ctx.output(recorded)
            ctx.evidence(current, hashes)
        else:
            current = ctx.source_file(recorded)
        require(digest(current) == checksum, "binary/source digest mismatch")
    for name in ("nuttx/nuttx", "nuttx/nuttx.bin", f"cargo/{TARGET}/release/event-demo",
                 "provider-selection.json", "resolved.config"):
        require(str(ctx.original_out / name) in binary_paths, "missing required binary digest")
    summary.update(target_packages=sorted(target_packages), consumed_configuration=consumed)
    return dict(sorted(hashes.items())), summary


def finish_case(case: str, out: Path, source_root: Path, revision: str) -> dict[str, str]:
    return _finished(case, Context(source_root.resolve(), source_root, out.resolve(), out), revision)[0]


def check_report(report: dict, source_root: Path, outputs: dict[str, Path] | None = None) -> dict:
    require(report["schema"] == 1 and report["clock"] == "monotonic_ns", "wrong witness schema/clock")
    require(set(report["cases"]) == set(CASES), "wrong witness case inventory")
    source = report["source"]
    require(isinstance(source["revision"], str) and HEX40.fullmatch(source["revision"])
            and isinstance(source["tree"], str) and HEX40.fullmatch(source["tree"]), "invalid source identity")
    original_source = absolute(source["root"], "source checkout")
    source_root = source_root.resolve()
    git = lambda *args: subprocess.check_output(["git", "-C", str(source_root), *args], text=True).strip()
    require(git("rev-parse", "HEAD") == source["revision"]
            and git("rev-parse", "HEAD^{tree}") == source["tree"], "source checkout differs from witnessed revision")
    require(not git("status", "--porcelain", "--untracked-files=all"), "source checkout is not clean")
    require(set(source["inputs"]) == set(SOURCE_INPUTS), "wrong source input inventory")
    for name, checksum in source["inputs"].items():
        require(sha(checksum) and digest(source_root / name) == checksum, "source input changed")
    original = {case: absolute(report["cases"][case]["output"], "case output") for case in CASES}
    require(original["bmi270"].parent == original["lsm6dso"].parent
            and all(original[case].name == case for case in CASES), "outputs must be distinct named siblings")
    outputs = outputs or original
    require(set(outputs) == set(CASES), "provide both relocated output roots")
    outputs = {case: Path(path).resolve() for case, path in outputs.items()}
    require(outputs["bmi270"].parent == outputs["lsm6dso"].parent
            and all(outputs[case].name == case for case in CASES), "relocated outputs must be distinct named siblings")
    parent, original_parent = outputs["bmi270"].parent, original["bmi270"].parent
    require(not original_source.is_relative_to(original_parent)
            and original_parent != original_source, "witness output overlaps source root")
    def global_file(record):
        path = absolute(record["path"], "frontend evidence")
        require(path.is_relative_to(original_parent), "frontend evidence outside witness output")
        local = parent / path.relative_to(original_parent)
        require(local.is_file() and not local.is_symlink() and local.resolve().is_relative_to(parent)
                and sha(record["sha256"]) and digest(local) == record["sha256"], "frontend evidence changed")
        return local
    frontend = report["frontend"]
    global_file(frontend)
    require(frontend["path"] == str(original_parent / "frontend/debug/nxrs-build"), "wrong frontend artifact")
    global_file(frontend["build_log"])
    frontend_messages = global_file(frontend["cargo_messages"])
    require(frontend["build_log"]["path"] == str(original_parent / "frontend-build.log")
            and frontend["cargo_messages"]["path"] == str(original_parent / "frontend-messages.jsonl"),
            "wrong frontend log ownership")
    require(frontend["build_argv"] == ["cargo", "+1.90.0", "build", "--locked", "--manifest-path",
            str(original_source / "Cargo.toml"), "-p", "nxrs-build", "--bin", "nxrs-build",
            "--message-format=json-render-diagnostics", "--target-dir", str(original_parent / "frontend")],
            "frontend was not built with the prescribed Cargo command")
    compiler = frontend["compiler"]
    absolute(compiler["path"], "host compiler")
    require(sha(compiler["sha256"]) and compiler["version"].startswith("rustc 1.90.0 ")
            and "host: x86_64-unknown-linux-gnu" in compiler["version"], "wrong host compiler provenance")
    require(frontend["cargo_version"].startswith("cargo 1.90.0 "), "wrong host Cargo provenance")
    messages = [json.loads(line) for line in frontend_messages.read_text().splitlines()]
    roots = [row for row in messages if row.get("reason") == "compiler-artifact"
             and row.get("executable") == frontend["path"]]
    require(len(roots) == 1 and roots[0]["fresh"] is False
            and package_name(roots[0]) == "nxrs-build"
            and roots[0]["target"]["src_path"] == str(original_source / "tools/build/src/main.rs"),
            "missing fresh source-bound frontend artifact")
    require(sum(row.get("reason") == "build-finished" and row.get("success") is True
                for row in messages) == 1
            and not any(row.get("reason") == "build-finished" and row.get("success") is not True
                        for row in messages), "frontend Cargo did not finish successfully")
    require(report["environment"] == {"CARGO_BUILD_JOBS": "2", "NXRS_NUTTX_BUILD_JOBS": "2"},
            "wrong bounded build environment")
    overlap = report["overlap"]
    begin, end = integer(overlap["start_ns"], "overlap start"), integer(overlap["end_ns"], "overlap end")
    require(begin < end and overlap["alive_before"] == list(CASES)
            and overlap["alive_after"] == list(CASES), "missing bracketed live-build overlap")
    pids = set()
    summaries = {}
    for case in CASES:
        item = report["cases"][case]
        pid = integer(item["pid"], "PID")
        require(pid > 0 and pid not in pids, "missing/distinct child PID")
        pids.add(pid)
        require(integer(item["started_ns"], "child start") <= begin < end
                <= integer(item["exited_ns"], "child exit"), "build lifetimes did not overlap preparation")
        require(type(item["returncode"]) is int and item["returncode"] == 0, "child build failed")
        expected = [frontend["path"], "--workspace-root", str(original_source), "build",
                    "--app", "event-demo", "--platform", "cores3"]
        if case == "lsm6dso":
            expected += ["--overlay", OVERLAY]
        expected += ["--out", str(original[case])]
        require(item["argv"] == expected, "wrong child frontend invocation")
        require(item["build_log"]["path"] == str(original_parent / f"{case}.log"),
                "wrong child build log ownership")
        global_file(item["build_log"])
        ctx = Context(original_source, source_root, original[case], outputs[case])
        prepared, _ = _prepared(case, ctx)
        require(item["prepared_files"] == prepared, "prepared inputs changed after observed overlap")
        artifacts, summary = _finished(case, ctx, source["revision"])
        require(item["artifacts"] == artifacts, "final artifact inventory/hash mismatch")
        require(artifacts["nxrs-nuttx-link"] == frontend["sha256"],
                "target linker controller differs from the source-bound frontend")
        summaries[case] = {**summary, "prepared_files": len(prepared), "artifact_files": len(artifacts)}
    require(summaries["bmi270"]["semantic_digest"] != summaries["lsm6dso"]["semantic_digest"]
            and summaries["bmi270"]["input_fingerprint"] != summaries["lsm6dso"]["input_fingerprint"],
            "the two resolved profiles are not distinct")
    return {"schema": 1, "status": "passed", "revision": source["revision"], "tree": source["tree"],
            "overlap_ns": end - begin, "scope": "simultaneously live prepared builds; not CPU parallelism",
            "cases": summaries}


def export_bundle(report: dict, source_root: Path, destination: Path) -> Path:
    """Publish only verified, replayable evidence; never copy whole SDK/Cargo trees."""
    destination = destination.absolute()
    require(not destination.exists() and not destination.is_symlink(), "evidence bundle already exists")
    check_report(report, source_root)
    original_parent = Path(report["cases"][CASES[0]]["output"]).parent
    require(destination.parent == original_parent and destination.name == "evidence",
            "bundle must be the fresh evidence directory beneath the witness output")
    temporary = Path(tempfile.mkdtemp(prefix=".evidence-", dir=destination.parent))
    try:
        for case in CASES:
            original = Path(report["cases"][case]["output"])
            for name in report["cases"][case]["artifacts"]:
                path = relative(name)
                copied = temporary / case / path
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original / path, copied)
        global_records = [report["frontend"], report["frontend"]["build_log"],
                          report["frontend"]["cargo_messages"],
                          *(report["cases"][case]["build_log"] for case in CASES)]
        for record in global_records:
            path = absolute(record["path"], "global evidence")
            copied = temporary / path.relative_to(original_parent)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copied)
        check_report(report, source_root, {case: temporary / case for case in CASES})
        (temporary / "concurrent-builds.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--bmi270-out", type=Path)
    parser.add_argument("--lsm6dso-out", type=Path)
    parser.add_argument("--validated-report", type=Path)
    args = parser.parse_args()
    if args.validated_report:
        args.validated_report.unlink(missing_ok=True)
    require(bool(args.bmi270_out) == bool(args.lsm6dso_out), "provide both output relocations")
    outputs = {"bmi270": args.bmi270_out, "lsm6dso": args.lsm6dso_out} if args.bmi270_out else None
    result = check_report(json_file(args.report), args.repo, outputs)
    if args.validated_report:
        args.validated_report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=args.validated_report.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, indent=2)
            stream.write("\n")
        temporary.replace(args.validated_report)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
