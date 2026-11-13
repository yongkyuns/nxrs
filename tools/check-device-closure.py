#!/usr/bin/env python3
"""Check finite CoreS3 linked-device qualifications, without building or resolving Cargo.

This is a finite qualification contract, not a general driver registry. Cargo's
normal target graph and compiler artifacts establish selection/exclusion; a
verified generated factory and retained ELF objects establish linked storage.
Missing Rust method names are not evidence of exclusion under fat LTO.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TARGET = "xtensa-esp32s3-nuttx"
PLATFORM = "m5stack-cores3-gnss"
APP = "nxrs-event-demo"
BIN = "event-demo"
BASE = "platform/cores3/profile.toml"
OVERLAY = "tests/host/fixtures/lsm6dso-overlay.toml"
BSP = "platform/nuttx/resources/esp32s3-m5-cores3.toml"
MATRIX_ROOT = "tests/nuttx-std/device-composition"
TWO_IMUS = MATRIX_ROOT + "/overlays/two-bmi270.toml"
MIXED_IMUS = MATRIX_ROOT + "/overlays/mixed-imu.toml"
CASES = {
    "bmi270": {"compatible": "bosch,bmi270", "implementation": "Bmi270",
               "bus": "sensor_bus", "path": "/dev/i2c0", "frequency": 400000,
               "address": 104, "other": "lsm6dso"},
    "lsm6dso": {"compatible": "st,lsm6dso", "implementation": "Lsm6dso",
                "bus": "alternate_i2c", "path": "/dev/i2c1", "frequency": 100000,
                "address": 106, "other": "bmi270"},
}
MATRIX_CASES = {
    "two-bmi270": {"consumer": "imu", "secondary": "bmi270", "overlays": (TWO_IMUS,)},
    "mixed-imu": {"consumer": "imu", "secondary": "lsm6dso", "overlays": (TWO_IMUS, MIXED_IMUS)},
    "gnss-only": {"consumer": "gnss", "secondary": "lsm6dso", "overlays": (TWO_IMUS, MIXED_IMUS)},
}
SHA = re.compile(r"[0-9a-f]{64}\Z")
OBJECT = re.compile(
    r"composition::(imu|gnss)_factory::(PRIMARY_DEVICE|DEVICE_[0-9]+)"
    r"(?:::h[0-9a-f]{16})?(?:\.llvm\.[0-9A-Fa-f]+)?\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def expected_case(case):
    """Fixed test contracts, independent of every receipt being checked.

    This is deliberately limited to the original pair and three qualification
    fixtures. It does not resolve profiles, discover capabilities or load policy
    from a build's output.
    """
    require(case in CASES or case in MATRIX_CASES, f"unknown qualification case: {case}")
    app = {"package": APP, "bin": BIN, "manifest": "app/event-demo/Cargo.toml", "command": "event_demo"}
    capabilities = {"gnss", "imu"}
    aliases = {"gnss": "navigation", "imu": "imu"}
    resources = {"receiver_uart": {"kind": "uart", "device": "/dev/ttyS1", "baud": 38400},
                 "sensor_bus": {"kind": "i2c", "device": "/dev/i2c0", "frequency": 400000}}
    if case in CASES:
        imus = [("imu", "PRIMARY_DEVICE", case, CASES[case])]
        overlays = (OVERLAY,) if case == "lsm6dso" else ()
        if case == "lsm6dso":
            resources["alternate_i2c"] = {"kind": "i2c", "device": "/dev/i2c1", "frequency": 100000}
    else:
        matrix = MATRIX_CASES[case]
        cap, secondary = matrix["consumer"], matrix["secondary"]
        app = {"package": "nxrs-device-composition-" + cap, "bin": "device-composition-" + cap,
               "manifest": MATRIX_ROOT + "/" + cap + "/Cargo.toml", "command": "device_composition_" + cap}
        capabilities, overlays = {cap}, matrix["overlays"]
        imus = [("imu", "PRIMARY_DEVICE", "bmi270", CASES["bmi270"]),
                ("secondary", "DEVICE_1", secondary,
                 {**CASES[secondary], "bus": "sensor_bus", "path": "/dev/i2c0", "frequency": 400000,
                  "address": 105 if secondary == "bmi270" else 106})]
        aliases |= {"imu_primary_alias": "imu", "imu_secondary": "secondary", "gnss_primary_alias": "navigation"}
    devices = {"navigation": {"compatible": "u-blox,neo-m9n", "bus": "receiver_uart"}}
    declarations = {}
    for identifier, instance, driver, model in imus:
        devices[identifier] = {"compatible": model["compatible"], "bus": model["bus"],
                               "address": model["address"], "wake": {"kind": "periodic"}}
        delay = ", resources::Delay" if driver == "bmi270" else ""
        declarations.setdefault("imu", []).append(
            f'declare_device!({instance}, "{identifier}", ::r#{driver}::{model["implementation"]}'
            f'<resources::Bus{delay}>, resources::I2cResource, resources::open_i2c, '
            f'(resource::Config {{ path: "{model["path"]}", frequency: {model["frequency"]} }}, '
            f'{model["address"]}), None, 16384, true);')
    declarations["gnss"] = ['declare_device!(PRIMARY_DEVICE, "navigation", ::r#ubx::NeoM9n<Serial>, '
                            '"/dev/ttyS1", 38400, 16384, false);']
    accessors = {"imu": {"imu": "PRIMARY_DEVICE", "primary_imu": "PRIMARY_DEVICE"},
                 "gnss": {"navigation": "PRIMARY_DEVICE", "primary_gnss": "PRIMARY_DEVICE"}}
    if case in MATRIX_CASES:
        accessors["imu"] |= {"primary_alias": "PRIMARY_DEVICE", "secondary": "DEVICE_1"}
        accessors["gnss"]["primary_alias"] = "PRIMARY_DEVICE"
    generated = {cap: "// Generated canonical device instances.\n" + "\n".join(declarations[cap])
                 + "\npub mod devices {\n" + "".join(
                     f"pub fn r#{name}() -> &'static impl super::Device {{ &super::{instance} }}\n"
                     for name, instance in sorted(accessors[cap].items())) + "}\n"
                 for cap in capabilities}
    drivers = {driver for _, _, driver, _ in imus} if "imu" in capabilities else set()
    features = drivers | ({"i2c"} if "imu" in capabilities else set()) | ({"ubx"} if "gnss" in capabilities else set())
    required = {app["package"], "nxrs-composition", "nxrs-acquisition"} | {"nxrs-" + cap for cap in capabilities}
    required |= {"nxrs-" + driver for driver in drivers}
    required |= {"nxrs-i2c", "nxrs-gpio"} if "imu" in capabilities else set()
    if "gnss" in capabilities:
        required.add("nxrs-ubx-driver")
    forbidden = {"nxrs-spi"} | {"nxrs-" + driver for driver in {"bmi270", "lsm6dso"} - drivers}
    if "bmi270" not in drivers:
        forbidden |= {"bmi2", "fixedvec"}
    if "gnss" not in capabilities:
        forbidden |= {"nxrs-gnss", "nxrs-ubx-driver", "kinavis-ubx", "kinavis-kernel"}
    if "imu" not in capabilities:
        forbidden |= {"nxrs-imu", "nxrs-i2c", "nxrs-gpio"}
    native = {"nxrs-acquisition"} | ({"nxrs-gpio", "nxrs-i2c"} if "imu" in capabilities else set())
    native |= {"nxrs-gnss"} if "gnss" in capabilities else set()
    objects = [("imu", instance) for _, instance, _, _ in imus] if "imu" in capabilities else []
    if "gnss" in capabilities:
        objects.append(("gnss", "PRIMARY_DEVICE"))
    helpers = {"i2c_resource_transfer"} if "imu" in capabilities else {"gnss_serial_configure", "gnss_serial_restore"}
    return {"app": app, "capabilities": capabilities, "devices": devices, "aliases": aliases,
            "resources": resources, "overlays": overlays, "features": features, "drivers": drivers,
            "required": required, "forbidden": forbidden, "native": native, "generated": generated,
            "objects": sorted(objects), "helpers": helpers}


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def json_digest(value):
    # serde_json::Value uses sorted object keys in the receipt producer.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def command(args, cwd=None):
    return subprocess.check_output([str(a) for a in args], cwd=cwd, text=True,
                                   env={**os.environ, "LC_ALL": "C"})


def hash_list(text):
    hashes = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, "malformed success/download hash manifest")
        value, name = match.groups()
        path = Path(name)
        require(path.is_absolute() and ".." not in path.parts,
                f"noncanonical hash-manifest path: {name}")
        require(name not in hashes or hashes[name] == value, f"conflicting hashes for {name}")
        hashes[name] = value
    require(hashes, "empty success/download hash manifest")
    return hashes


def package_name(artifact):
    identity = artifact["package_id"]
    prefix, separator, fragment = identity.rpartition("#")
    require(separator and fragment, f"invalid Cargo package identity: {identity}")
    if "@" in fragment:
        return fragment.rsplit("@", 1)[0]
    # Cargo omits a redundant name for e.g. the private std source package.
    require(prefix.startswith("path+file://"), f"unnamed non-path Cargo package: {identity}")
    return Path(unquote(urlsplit(prefix[5:]).path)).name


def normal_packages(selection):
    resolution = selection["dependency_resolution"]
    require(resolution["status"] == "resolved", "Cargo target dependency graph is not resolved")
    result = {}
    for description in resolution["packages"]:
        match = re.fullmatch(r"(\S+) v(\S+)(?: \((.+)\))?", description)
        require(match is not None, f"invalid Cargo package description: {description}")
        name, version, source = match.groups()
        require(name not in result, f"ambiguous package in finite target graph: {name}")
        result[name] = {"version": version, "source": source, "description": description}
    return result


def artifact_scopes(records, names, recorded_out, out):
    """Reuse the existing artifact classifier, with one explicit output mapping."""
    spec = importlib.util.spec_from_file_location("hal_artifacts", ROOT / "tools/check-hal-isolation.py")
    classifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(classifier)
    mapped = []
    for record in records:
        record = dict(record)
        if record.get("reason") == "compiler-artifact":
            paths = []
            for name in record["filenames"]:
                path = Path(name)
                require(path.is_relative_to(recorded_out), "Cargo artifact belongs to another output")
                paths.append(str(out / path.relative_to(recorded_out)))
            record["filenames"] = paths
        mapped.append(record)
    try:
        return classifier.packages("\n".join(json.dumps(r) for r in mapped), names,
                                   out / "cargo", TARGET)
    except AssertionError as error:
        raise ValueError(f"invalid host/target Cargo evidence: {error}") from error


class Evidence:
    def __init__(self, out, root, case="bmi270"):
        self.out, self.root = Path(out).resolve(), Path(root).resolve()
        self.expected = expected_case(case)
        app = self.expected["app"]
        self.hashes = {}
        self.receipt = self.read_json("resolved-profile.json")
        self.workspace = Path(self.receipt["workspace"])
        require(self.workspace.is_absolute(), "receipt workspace is not absolute")
        self.selection = self.read_json("provider-selection.json")
        text = self.read("cargo-messages.jsonl")
        self.records = [json.loads(line) for line in text.splitlines() if line.strip()]
        require(self.records and self.records[-1] == {"reason": "build-finished", "success": True},
                "missing successful final Cargo build-finished record")
        self.artifacts = [r for r in self.records if r.get("reason") == "compiler-artifact"]
        apps = [r for r in self.artifacts if package_name(r) == app["package"]
                and r["target"]["kind"] == ["bin"] and r["target"]["name"] == app["bin"]]
        require(len(apps) == 1, f"expected exactly one {app['bin']} executable artifact")
        self.app = apps[0]
        executable = Path(self.app["executable"])
        require(executable.is_absolute() and len(executable.parents) >= 4,
                "invalid Cargo executable path")
        self.recorded_out = executable.parents[3]
        require(executable == self.recorded_out / "cargo" / TARGET / "release" / app["bin"],
                "application artifact is not in the selected target release directory")
        require(self.app["filenames"] == [str(executable)], "inconsistent executable artifact")
        self.success = hash_list(self.read("binaries.sha256"))

    def path(self, relative):
        result = (self.out / relative).resolve()
        require(result.is_relative_to(self.out), f"evidence escapes its output: {relative}")
        require(result.is_file(), f"missing evidence: {result}")
        return result

    def read(self, relative):
        path = self.path(relative)
        self.hashes[str(path.relative_to(self.out))] = digest(path)
        return path.read_text()

    def read_json(self, relative):
        return json.loads(self.read(relative))

    def mapped(self, recorded):
        path = Path(recorded)
        require(path.is_absolute() and ".." not in path.parts, f"invalid recorded path: {path}")
        for old, new in ((self.recorded_out, self.out), (self.workspace, self.root)):
            if path.is_relative_to(old):
                result = (new / path.relative_to(old)).resolve()
                require(result.is_relative_to(new), f"recorded path escapes mapped root: {path}")
                require(result.is_file(), f"missing recorded input: {result}")
                return result
        raise ValueError(f"recorded path has no admitted workspace/output mapping: {path}")

    def verified(self, recorded, expected=None):
        expected = self.success.get(str(recorded)) if expected is None else expected
        require(isinstance(expected, str) and SHA.fullmatch(expected),
                f"missing success/input hash: {recorded}")
        path = self.mapped(recorded)
        require(digest(path) == expected, f"stale or mismatched evidence: {path}")
        if path.is_relative_to(self.out):
            self.hashes[str(path.relative_to(self.out))] = expected
        return path


def validate_selection(evidence, case, revision):
    e, expected = evidence, evidence.expected
    app = expected["app"]
    r, s = e.receipt, e.selection
    require(r["version"] == 1 and r["profile"] == PLATFORM and r["target"] == TARGET
            and r["os"] == "nuttx", "wrong resolved profile/target receipt")
    require(s["app_package"] == app["package"] and s["platform"] == PLATFORM
            and s["target_os"] == "nuttx" and s["nuttx-build-mode"] == "flat",
            "wrong application/platform/provider selection")
    manifest = str(e.workspace / app["manifest"])
    require(s["app_manifest"] == manifest == r["product_manifest"] == e.app["manifest_path"],
            "application manifest differs across evidence")
    require(r["data"]["board"] == "esp32s3-m5-cores3:nsh"
            and r["data"]["target"] == TARGET and r["data"]["build"]["size-optimized"] is True,
            "wrong board or optimization profile")
    require(s["providers"] == [
        {"capability": c, "implementation": "nuttx", "package": "nxrs-" + c}
        for c in ("gnss", "imu")], "wrong declared capability providers")
    composition = s["resolved-composition"]
    graph = r["graph"]
    require(composition["graph"] == graph, "provider/receipt composition mismatch")
    require(composition["semantic-digest"] == r["semantic_digest"] == json_digest({
        "graph": graph, "target": r["data"].get("target"),
        "nuttx-resources": r["data"].get("resolved-nuttx-resources")}), "semantic digest mismatch")
    require(composition["input-fingerprint"] == r["raw_fingerprint"]
            and SHA.fullmatch(r["raw_fingerprint"]), "input fingerprint mismatch")
    require(graph["version"] == 1 and graph["mode"] == "hardware", "wrong composition mode")
    require(graph["devices"] == expected["devices"],
        f"wrong {case} device model, bus or endpoint")
    require(graph["aliases"] == expected["aliases"], "wrong primary aliases")
    require(graph["resources"] == expected["resources"], "unexpected declared resource graph")
    require(graph["stack_bytes"] == graph["gnss_stack_bytes"] == 16384,
            "unexpected acquisition stack configuration")
    # Receipts preserve CLI spelling; the resolver interprets relative overlays
    # against the workspace and records canonical paths separately in profiles.
    # Apply that interpretation after relocating the recorded workspace, without
    # accepting another file merely because its basename or content matches.
    require(isinstance(r["cli_overlays"], list)
            and all(isinstance(value, str) for value in r["cli_overlays"]),
            "overlay receipt must be an array of path strings")
    cli_overlays = []
    for value in r["cli_overlays"]:
        recorded = e.workspace / value
        require(recorded.is_relative_to(e.workspace), "overlay is outside the recorded workspace")
        local = (e.root / recorded.relative_to(e.workspace)).resolve()
        require(local.is_relative_to(e.root), "overlay escapes the mapped workspace")
        cli_overlays.append(local)
    overlays = [str(e.workspace / name) for name in expected["overlays"]]
    expected_overlays = [(e.root / name).resolve() for name in expected["overlays"]]
    require(cli_overlays == expected_overlays, "wrong overlay receipt")
    profiles = {str(e.workspace / name) for name in (BASE, BSP)} | set(overlays)
    require({p["path"] for p in s["profiles"]} == profiles, "wrong profile source set")
    dependencies = {}
    for item in r["dependencies"]:
        require(item["path"] not in dependencies, "duplicate receipt dependency")
        require(e.success.get(item["path"]) == item["sha256"], "receipt/success input hash mismatch")
        e.verified(item["path"], item["sha256"])
        dependencies[item["path"]] = item["sha256"]
    require(profiles | {manifest, str(e.workspace / "Cargo.toml")} <= dependencies.keys(),
            "receipt omits required profile/product inputs")
    for item in s["profiles"]:
        require(dependencies.get(item["path"]) == item["sha256"], "profile source hash mismatch")
    require(set(composition["binding-inputs"]) == {"imu", "gnss"}, "wrong declared capability catalogues")
    for cap in ("imu", "gnss"):
        catalog = composition["binding-inputs"][cap]
        require(catalog["package"] == "nxrs-composition"
                and catalog["manifest"]["path"] == str(e.workspace / "platform/Cargo.toml"),
                "unexpected composition catalogue")
        for item in [catalog["manifest"], *catalog["bindings"], *catalog["dependencies"]]:
            require(dependencies.get(item["path"]) == item["sha256"], "catalogue input hash mismatch")
    for name in ("provider-selection.json", "resolved.config", TARGET + ".json"):
        e.verified(e.recorded_out / name)
    spec = e.read_json(TARGET + ".json")
    require(spec["arch"] == "xtensa" and spec["os"] == "nuttx"
            and spec["target-pointer-width"] == "32", "wrong compiled target specification")
    provenance = e.read("provenance.txt")
    fields = {}
    for line in provenance.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", key) and not key.startswith("kconfig_") and key != "target_c_source":
            require(key not in fields, f"duplicate provenance field: {key}")
            fields[key] = value
    for key, value in {"nxrs": revision, "nxrs_dirty": "no", "app_package": app["package"],
                       "app_bin": app["bin"], "platform": PLATFORM, "target": TARGET,
                       "board": "esp32s3-m5-cores3:nsh", "nuttx_build_mode": "flat",
                       "size_optimized": "1", "app_command": app["command"]}.items():
        require(fields.get(key) == value, f"wrong/stale provenance {key}")
    compiler = [line for line in provenance.splitlines() if line.startswith((
        "rustc ", "commit-hash: ", "commit-date: ", "host: ", "release: ",
        "LLVM version: ", "xtensa-esp-elf-gcc "))]
    require(len(compiler) == 7, "incomplete target compiler provenance")
    return {"nuttx": fields["nuttx"], "apps": fields["apps"], "compiler": compiler,
            "source_inputs": {str(Path(p).relative_to(e.workspace)): h for p, h in dependencies.items()}}


def validate_cargo(e, case):
    normal = normal_packages(e.selection)
    expected = e.expected
    required, forbidden = expected["required"], expected["forbidden"]
    require(required <= normal.keys() and not forbidden.intersection(normal),
            "wrong selected/unselected driver or resource in normal Cargo graph")
    features = {"nxrs-composition/" + f for f in expected["features"]}
    for value in (e.selection, e.selection["dependency_resolution"]):
        require(set(value["driver_features"]) == features and value["model_features"] == [],
                "wrong derived driver/resource features")
    names = {a["package_id"]: package_name(a) for a in e.artifacts}
    scopes = artifact_scopes(e.records, names, e.recorded_out, e.out)
    target = [a for a in e.artifacts if all(Path(p).is_relative_to(e.recorded_out / "cargo" / TARGET)
                                          for p in a["filenames"])]
    observed = {names[a["package_id"]] for a in target}
    require(required <= observed and not forbidden.intersection(observed),
            "selected driver/resource missing from target artifacts, or unselected target contamination")
    by_name = {}
    for artifact in target:
        name = names[artifact["package_id"]]
        if not name.startswith("nxrs-"):
            continue
        require(name in normal and name not in by_name, f"unexpected/duplicate Nxrs target artifact: {name}")
        package = normal[name]
        source = Path(package["source"])
        require(source.is_relative_to(e.workspace) and artifact["manifest_path"] == str(source / "Cargo.toml"),
                f"wrong source for target artifact: {name}")
        identity = artifact["package_id"]
        require(identity == source.as_uri().replace("file:", "path+file:", 1)
                + "#" + name + "@" + package["version"], f"wrong Cargo package identity: {name}")
        by_name[name] = artifact
    require(set(by_name) == {name for name in normal if name.startswith("nxrs-")},
            "normal Nxrs target graph and compiler artifacts differ")
    composition = by_name["nxrs-composition"]
    require(set(composition["features"]) == expected["features"] | expected["capabilities"],
            "compiled composition has wrong driver/resource features")
    require(e.app["profile"]["test"] is False and e.app["profile"]["opt_level"] == "z"
            and e.app["profile"]["debuginfo"] == 0, "not the expected optimized application build")
    std = [a for a in target if a["target"]["name"] == "std" and "rlib" in a["target"]["crate_types"]]
    std_source = e.read_json("std-source.json")
    require(len(std) == 1 and std_source["selected_source_matches"] is True
            and std[0]["target"]["src_path"] == std_source["std_source"] == std_source["expected_source"]
            == str(e.recorded_out / "toolchain/lib/rustlib/src/rust/library/std/src/lib.rs"),
            "wrong target standard-library source")
    return by_name, {scope: sorted(names[i] for i in ids) for scope, ids in scopes.items()}


def validate_preparation(e, case, composition):
    expected = e.expected
    handoff = Path(e.selection["configuration"])
    require(handoff.parent == e.recorded_out / "configuration", "handoff belongs to another output")
    local = e.mapped(handoff)
    require(SHA.fullmatch(handoff.stem) and digest(local) == handoff.stem, "stale configuration handoff")
    selected = e.read_json(str(local.relative_to(e.out)))
    catalogue = str(e.workspace / "platform")
    require(set(selected) == {catalogue} and set(selected[catalogue]) == expected["capabilities"],
            "wrong prepared catalogue/capabilities")
    scripts = [r for r in e.records if r.get("reason") == "build-script-executed"
               and r["package_id"] == composition["package_id"]]
    require(len(scripts) == 1, "missing/duplicate composition build-script handoff")
    out_dir = Path(scripts[0]["out_dir"])
    require(out_dir.is_relative_to(e.recorded_out / "cargo" / TARGET / "release/build"),
            "composition was not prepared for the target build")
    for cap, prepared in selected[catalogue].items():
        require(SHA.fullmatch(prepared["identity"]), "invalid prepared identity")
        output = prepared["output"]
        require(output["file"] == cap + "_config.rs", "wrong prepared output name")
        generated = handoff.parent / prepared["identity"] / output["file"]
        source = e.verified(generated, output["sha256"]).read_text()
        require(source == expected["generated"][cap],
                f"wrong generated {cap} typed construction or canonical aliases")
        e.verified(out_dir / output["file"], output["sha256"])
    native = e.selection["native-resources"]
    packages = normal_packages(e.selection)
    found = set()
    for resource in native:
        matches = [name for name, value in packages.items() if value["description"] == resource["package"]]
        require(len(matches) == 1 and matches[0] not in found, "invalid native resource package")
        found.add(matches[0])
        path = Path(resource["archive"])
        require(path.is_relative_to(e.recorded_out / "native-resources") and path.suffix == ".a",
                "native resource archive belongs to another build")
        e.verified(path, resource["sha256"])
    require(found == expected["native"], "wrong native resource closure")
    return sorted(found)


def inspection_tools(e, run=command):
    link = e.read_json("rust-link.json")
    linker = Path(link["linker"])
    require(linker.name == "xtensa-esp32s3-elf-ld"
            and linker.is_relative_to(e.workspace / "target/qemu-tools/gcc"),
            "linker is not from the pinned Xtensa bundle")
    require("-r" in link["effective"] and "--gc-sections" in link["effective"],
            "missing relocatable Rust link invocation")
    outputs = [link["effective"][i + 1] for i, a in enumerate(link["effective"][:-1]) if a == "-o"]
    require(len(outputs) == 1 and Path(outputs[0]).parent
            == e.recorded_out / "cargo" / TARGET / "release/deps",
            "Rust link invocation belongs to another target output")
    downloads = hash_list(e.read("downloads.sha256"))
    installer = e.root / "tools/install-qemu-tools.sh"
    match = re.search(r"fetch_unpack gcc \\\n[^\n]+\n\s+([0-9a-f]{64})", installer.read_text())
    require(match is not None, "cannot read the checked-in compiler archive pin")
    archive = e.workspace / "target/qemu-tools/gcc.tar.xz"
    require(downloads.get(str(archive)) == match[1], "wrong compiler archive download receipt")
    e.verified(archive, match[1])
    tools, identities = {}, {}
    for suffix in ("ld", "readelf", "nm"):
        path = e.mapped(linker.with_name("xtensa-esp32s3-elf-" + suffix))
        require(path.is_relative_to(e.root / "target/qemu-tools/gcc"), "binutils escape the pinned bundle")
        tools[suffix] = path
        identities[suffix] = {"path": str(path), "sha256": digest(path),
                              "version": run([path, "--version"]).splitlines()[0]}
    identities["archive_sha256"] = match[1]
    identities["installer_sha256"] = digest(installer)
    return tools, identities


def elf_tables(text):
    sections, symbols = {}, []
    for line in text.splitlines():
        section = re.match(r"\s*\[\s*(\d+)\]\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)\s+"
                           r"[0-9a-fA-F]+\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+([A-Z]*)\s+", line)
        if section:
            index, name, kind, address, size, flags = section.groups()
            sections[index] = {"name": name, "type": kind, "address": int(address, 16),
                               "size": int(size, 16), "flags": flags}
        parts = line.split(None, 7)
        if len(parts) == 8 and re.fullmatch(r"[0-9]+:", parts[0]) and parts[6].isdigit():
            symbols.append({"name": parts[7].strip(), "address": int(parts[1], 16),
                            "size": int(parts[2], 0) if parts[2].startswith("0x") else int(parts[2]),
                            "type": parts[3], "section_index": parts[6]})
    return sections, symbols


def inspect_elf(path, tools, kind, case, run=command, machine="Tensilica Xtensa Processor", elf_class="ELF32"):
    expected = expected_case(case)
    require(path.read_bytes()[:4] == b"\x7fELF", f"not an ELF: {path}")
    text = run([tools["readelf"], "-W", "-h", "-S", "-s", "--demangle", path])
    for field, value in (("Type", kind), ("Class", elf_class), ("Machine", machine)):
        require(re.search(r"^\s*" + field + r":\s+" + re.escape(value) + r"(?:\s|$)", text, re.M),
                f"wrong {field} in {path}")
    sections, symbols = elf_tables(text)
    live = [s for s in symbols if s["section_index"] in sections
            and "A" in sections[s["section_index"]]["flags"]]
    objects = []
    for symbol in live:
        match = OBJECT.fullmatch(symbol["name"])
        if match:
            section = sections[symbol["section_index"]]
            require(symbol["type"] == "OBJECT" and symbol["size"] > 0
                    and "X" not in section["flags"],
                    "primary device symbol is not nonzero static data")
            require(section["address"] <= symbol["address"]
                    and symbol["address"] + symbol["size"] <= section["address"] + section["size"],
                    "linked device object lies outside its allocated section")
            objects.append({"capability": match[1], "instance": match[2], "symbol": symbol["name"],
                            "address": hex(symbol["address"]), "size": symbol["size"],
                            "section": sections[symbol["section_index"]]["name"]})
    require(sorted((o["capability"], o["instance"]) for o in objects) == expected["objects"],
            "missing/duplicate/unexpected linked primary device objects")
    # Relocatable sections can each start at zero. Final device addresses must
    # prove distinct storage even when two objects use the same concrete type.
    if kind == "EXEC":
        ranges = sorted((int(o["address"], 16), o["size"]) for o in objects)
        require(all(address > 0 for address, _ in ranges)
                and all(address + size <= following for (address, size), (following, _) in zip(ranges, ranges[1:])),
                "overlapping or zero-address final device objects")
    required = expected["helpers"] | ({"main"} if kind == "REL" else {
        expected["app"]["command"] + "_main", "nx_start"})
    functions = {s["name"] for s in live if s["type"] == "FUNC"
                 and "X" in sections[s["section_index"]]["flags"]}
    require(required <= functions, "missing retained native helper or application entry")
    # nm independently confirms the defined-symbol view; no saved text is trusted.
    nm = run([tools["nm"], "--defined-only", "--demangle", path])
    nm_names = {line.split(None, 2)[2] for line in nm.splitlines() if len(line.split(None, 2)) == 3}
    require(required <= nm_names, "nm/readelf disagree on retained native/application functions")
    prefixes = {"nxrs_" + driver + "::" for driver in {"bmi270", "lsm6dso"} - expected["drivers"]}
    excluded_helpers = {"spi_resource_transfer"}
    if "gnss" not in expected["capabilities"]:
        prefixes |= {"gnss::", "nxrs_ubx_driver::"}
        excluded_helpers |= {"gnss_serial_" + name for name in ("nonblocking_flag", "capture", "configure", "restore")}
    if "imu" not in expected["capabilities"]:
        prefixes.add("imu::")
        excluded_helpers.add("i2c_resource_transfer")
    unexpected = [s["name"] for s in live if any(prefix in s["name"] for prefix in prefixes)
                  or s["name"] in excluded_helpers]
    require(not unexpected, f"unexpected retained driver/SPI symbol: {unexpected[:3]}")
    selected = expected["drivers"] | ({"ubx_driver"} if case in MATRIX_CASES and "gnss" in expected["capabilities"] else set())
    observed = sorted({s["name"] for s in live if any("nxrs_" + driver + "::" in s["name"] for driver in selected)})
    return {"sha256": digest(path), "type": kind, "class": elf_class, "machine": machine,
            "static_objects": sorted(objects, key=lambda o: (o["capability"], o["instance"])),
            "static_object_count": len(objects), "static_object_bytes": sum(o["size"] for o in objects),
            "retained_functions": sorted(required), "named_driver_symbol_count": len(observed),
            "named_driver_symbol_examples": observed[:8],
            "allocated_sections": [{k: v for k, v in section.items() if k != "address"}
                                   for section in sections.values() if "A" in section["flags"]]}


def check_case(out, root, case, revision, run=command):
    e = Evidence(out, root, case)
    provenance = validate_selection(e, case, revision)
    artifacts, scopes = validate_cargo(e, case)
    native = validate_preparation(e, case, artifacts["nxrs-composition"])
    tools, identities = inspection_tools(e, run)
    rust_hash = e.success.get(e.app["executable"])
    require(isinstance(rust_hash, str) and SHA.fullmatch(rust_hash), "missing Cargo executable success hash")
    rust = e.verified(e.recorded_out / "rust-input.elf", rust_hash)
    # Cargo's release executable is a second name for the copied Rust input.
    if (e.out / "cargo" / TARGET / "release" / e.expected["app"]["bin"]).is_file():
        e.verified(e.app["executable"])
    reuse = e.read_json("link-reuse.json")
    pruning = e.read_json("rust-symbol-pruning.json")
    require(reuse["artifact_sha256"] == pruning["after_sha256"] == rust_hash,
            "Rust linker/reuse evidence does not match the Cargo executable")
    final = e.verified(e.recorded_out / "nuttx/nuttx")
    # Preserve map identities for later inspection. Discarded/input names in a
    # map are deliberately not used to infer retained code or driver absence.
    for name in ("rust-partial.map", "nuttx/nuttx.map"):
        require(e.read(name).strip(), f"empty retained map: {name}")
    rust_report = inspect_elf(rust, tools, "REL", case, run)
    final_report = inspect_elf(final, tools, "EXEC", case, run)
    require([(s["capability"], s["instance"], s["size"]) for s in rust_report["static_objects"]]
            == [(s["capability"], s["instance"], s["size"]) for s in final_report["static_objects"]],
            "Rust and final linked device storage differ")
    result = {"output": str(e.out),
            "semantic_digest": e.receipt["semantic_digest"], "provenance": provenance,
            "cargo_packages": scopes, "driver_features": e.selection["driver_features"],
            "native_resources": native, "tools": identities, "rust_elf": rust_report,
            "final_elf": final_report, "evidence_sha256": dict(sorted(e.hashes.items()))}
    if case in CASES:
        result["compatible"] = CASES[case]["compatible"]
    else:
        result |= {"application": e.expected["app"]["package"],
                   "consumed_capabilities": sorted(e.expected["capabilities"]),
                   "declared_devices": e.expected["devices"],
                   "declared_device_count": len(e.expected["devices"]),
                   "consumed_device_count": len(e.expected["objects"])}
    return result


def validate_checkout(root, revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "expected a full checkout revision")
    require(command(["git", "rev-parse", "HEAD"], root).strip() == revision, "checkout revision mismatch")
    require(not command(["git", "status", "--porcelain", "--untracked-files=no"], root).strip(),
            "tracked checkout is dirty")


def validate_common(cases, root, label):
    first, *others = cases.values()
    for other in others:
        for field in ("nuttx", "apps", "compiler"):
            require(first["provenance"][field] == other["provenance"][field], f"{label} has different {field} provenance")
        require(first["tools"] == other["tools"], f"{label} uses different inspection tool identities")
    for key, submodule in (("nuttx", "nuttx"), ("apps", "nuttx-apps")):
        require(first["provenance"][key] == command(["git", "rev-parse", ":external/" + submodule], root).strip(),
                f"wrong pinned {key} source revision")


def check_pair(bmi270_out, lsm6dso_out, revision, root=ROOT, run=command):
    root = Path(root).resolve()
    validate_checkout(root, revision)
    require(Path(bmi270_out).resolve() != Path(lsm6dso_out).resolve(), "pair requires distinct build outputs")
    cases = {name: check_case(out, root, name, revision, run)
             for name, out in (("bmi270", bmi270_out), ("lsm6dso", lsm6dso_out))}
    a, b = (cases[name] for name in CASES)
    validate_common(cases, root, "pair")
    b_sources = dict(b["provenance"]["source_inputs"])
    require(b_sources.pop(OVERLAY, None) is not None and b_sources == a["provenance"]["source_inputs"],
            "pair source inputs differ beyond the declared overlay")
    require(a["semantic_digest"] != b["semantic_digest"]
            and a["rust_elf"]["sha256"] != b["rust_elf"]["sha256"]
            and a["final_elf"]["sha256"] != b["final_elf"]["sha256"], "pair reuses identical case evidence")
    return {"version": 1, "revision": revision, "target": TARGET, "platform": PLATFORM,
            "scope": "Two existing CoreS3 event-demo builds; Cargo target selection, verified typed factories, "
                     "and observed linked static objects. Missing symbols alone do not prove driver exclusion. "
                     "Object bytes exclude worker stacks, queues, heap and OS allocations; no total-RAM or physical claim.",
            "cases": cases,
            "static_object_bytes_delta_lsm6dso_minus_bmi270":
                b["final_elf"]["static_object_bytes"] - a["final_elf"]["static_object_bytes"]}


def check_matrix(same_model_out, mixed_out, gnss_only_out, revision, root=ROOT, run=command):
    root = Path(root).resolve()
    validate_checkout(root, revision)
    outputs = dict(zip(MATRIX_CASES, (same_model_out, mixed_out, gnss_only_out)))
    require(len({Path(out).resolve() for out in outputs.values()}) == 3, "matrix requires distinct build outputs")
    cases = {name: check_case(out, root, name, revision, run) for name, out in outputs.items()}
    validate_common(cases, root, "matrix")
    fixture_inputs = {MATRIX_ROOT + "/" + cap + "/Cargo.toml" for cap in ("imu", "gnss")} | {TWO_IMUS, MIXED_IMUS}
    common_inputs = []
    for case, result in cases.items():
        sources = result["provenance"]["source_inputs"]
        expected = expected_case(case)
        require(sources.keys() & fixture_inputs == {expected["app"]["manifest"], *expected["overlays"]},
                "matrix source inputs have wrong fixture manifest or overlays")
        common_inputs.append({p: h for p, h in sources.items() if p not in fixture_inputs})
    require(all(inputs == common_inputs[0] for inputs in common_inputs[1:]),
            "matrix source inputs differ beyond the declared fixture manifests and overlays")
    same, mixed, gnss = (cases[name] for name in MATRIX_CASES)
    require(same["semantic_digest"] != mixed["semantic_digest"] == gnss["semantic_digest"],
            "matrix declared device graphs do not match the fixed overlay contracts")
    for kind in ("rust_elf", "final_elf"):
        require(len({case[kind]["sha256"] for case in cases.values()}) == 3,
                "matrix reuses identical case evidence")
    sizes = {name: {(obj["capability"], obj["instance"]): obj["size"] for obj in result["final_elf"]["static_objects"]}
             for name, result in cases.items()}
    require(sizes["two-bmi270"][("imu", "PRIMARY_DEVICE")] == sizes["two-bmi270"][("imu", "DEVICE_1")]
            == sizes["mixed-imu"][("imu", "PRIMARY_DEVICE")], "same concrete BMI270 device type has inconsistent storage")
    return {"version": 1, "revision": revision, "target": TARGET, "platform": PLATFORM,
            "scope": "Three qualification-only CoreS3 images; same-model and mixed IMU instances, canonical aliases, "
                     "and declared unconsumed capabilities. Cargo target selection, verified typed factories and observed "
                     "linked static objects establish closure; missing symbols alone do not prove exclusion. "
                     "Object bytes exclude worker stacks, queues, heap and OS allocations; no total-RAM or physical claim.",
            "cases": cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bmi270-out", type=Path)
    parser.add_argument("--lsm6dso-out", type=Path)
    parser.add_argument("--same-model-out", type=Path)
    parser.add_argument("--mixed-out", type=Path)
    parser.add_argument("--gnss-only-out", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    args.report.unlink(missing_ok=True)
    try:
        pair = [args.bmi270_out, args.lsm6dso_out]
        matrix = [args.same_model_out, args.mixed_out, args.gnss_only_out]
        require((all(pair) and not any(matrix)) or (all(matrix) and not any(pair)),
                "supply either the complete original pair or the complete three-case matrix")
        report = check_pair(*pair, args.revision) if all(pair) else check_matrix(*matrix, args.revision)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=args.report.parent, prefix=args.report.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(report, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                os.replace(temporary, args.report)
            finally:
                temporary.unlink(missing_ok=True)
        print(f"PASS: {'paired' if all(pair) else 'matrix'} CoreS3 linked device evidence: {args.report}")
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"FAIL: device closure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
