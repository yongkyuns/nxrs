"""Adversarial evidence tests; the host ELF smoke test is not target qualification."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("device_closure", ROOT / "tools/check-device-closure.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
REVISION = "a" * 40
RECORDED = Path("/recorded/nxrs")
TARGET = "xtensa-esp32s3-nuttx"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def read_json(path):
    return json.loads(path.read_text())


def elf_text(kind, size):
    return f"""ELF Header:
  Class:                             ELF32
  Type:                              {kind} (fixture)
  Machine:                           Tensilica Xtensa Processor
Section Headers:
  [ 1] .text PROGBITS 42000000 000100 000100 00 AX 0 0 4
  [ 2] .data PROGBITS 3fc90000 000200 000400 00 WA 0 0 4
Symbol table '.symtab' contains 7 entries:
  1: 3fc90000 {size} OBJECT LOCAL DEFAULT 2 composition::imu_factory::PRIMARY_DEVICE
  2: 3fc90200 48 OBJECT LOCAL DEFAULT 2 composition::gnss_factory::PRIMARY_DEVICE
  3: 42000000 16 FUNC GLOBAL DEFAULT 1 i2c_resource_transfer
  4: 42000010 16 FUNC GLOBAL DEFAULT 1 main
  5: 42000020 16 FUNC GLOBAL DEFAULT 1 event_demo_main
  6: 42000030 16 FUNC GLOBAL DEFAULT 1 nx_start
"""


class DeviceClosureFixture(unittest.TestCase):
    """Producer-shaped JSON with injected binutils output for admission tests.

    A separate test invokes actual host binutils. Fabricated target-shaped
    inputs in these unit tests never constitute target qualification.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.outputs = {case: self.root / "builds" / case for case in ("bmi270", "lsm6dso")}
        self.elf_output = {}
        self.sources = ["Cargo.toml", "app/event-demo/Cargo.toml", "platform/Cargo.toml",
                        "platform/cores3/profile.toml", "platform/nuttx/resources/esp32s3-m5-cores3.toml"]
        self.sources += [f"driver/{driver}/{name}" for driver in ("bmi270", "lsm6dso", "ubx")
                         for name in ("Cargo.toml", "bindings.toml")]
        for name in self.sources + ["tests/host/fixtures/lsm6dso-overlay.toml"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture source: " + name)
        archive = self.root / "target/qemu-tools/gcc.tar.xz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"test compiler archive")
        installer = self.root / "tools/install-qemu-tools.sh"
        installer.parent.mkdir()
        installer.write_text("fetch_unpack gcc \\\n  https://fixture.invalid/gcc.tar.xz \\\n  "
                             + CHECK.digest(archive) + "\n")
        for suffix in ("ld", "nm", "readelf"):
            tool = self.root / ("target/qemu-tools/gcc/bin/xtensa-esp32s3-elf-" + suffix)
            tool.parent.mkdir(parents=True, exist_ok=True)
            tool.write_text("test binutils " + suffix)
        for case in self.outputs:
            self.make_case(case)

    def put(self, case, name, value):
        path = self.outputs[case] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else value.encode())
        return path

    def recorded_out(self, case):
        group = "event-demo" if case in ("bmi270", "lsm6dso") else "device-composition"
        return RECORDED / "target/firmware" / group / case

    def source_hash(self, name):
        return {"path": str(RECORDED / name), "sha256": CHECK.digest(self.root / name)}

    def artifact(self, case, name, source, scope="target", kind="lib"):
        directory = self.recorded_out(case) / "cargo"
        if scope == "target":
            directory /= TARGET
        filename = directory / "release/deps" / (name.replace("-", "_") + ".rlib")
        return {"reason": "compiler-artifact", "package_id": source.as_uri().replace("file:", "path+file:", 1)
                + "#" + name + "@0.1.0", "manifest_path": str(source / "Cargo.toml"),
                "target": {"name": name.replace("-", "_"), "kind": [kind], "crate_types": [kind],
                           "src_path": str(source / "src/lib.rs")},
                "profile": {"test": False, "opt_level": "z", "debuginfo": 0},
                "features": [], "filenames": [str(filename)], "executable": None, "fresh": False}

    def make_case(self, case):
        out, old = self.outputs[case], self.recorded_out(case)
        out.mkdir(parents=True)
        matrix = case in ("two-bmi270", "mixed-imu", "gnss-only")
        bmi = case != "lsm6dso"
        caps = {"imu", "gnss"} if not matrix else {"gnss" if case == "gnss-only" else "imu"}
        consumer = "gnss" if case == "gnss-only" else "imu"
        app_package = "nxrs-device-composition-" + consumer if matrix else "nxrs-event-demo"
        app_bin = "device-composition-" + consumer if matrix else "event-demo"
        app_path = "tests/nuttx-std/device-composition/" + consumer if matrix else "app/event-demo"
        drivers = {"bmi270" if bmi else "lsm6dso"}
        if case == "mixed-imu":
            drivers.add("lsm6dso")
        if case == "gnss-only":
            drivers = set()
        imu = {"compatible": "bosch,bmi270" if bmi else "st,lsm6dso",
               "bus": "sensor_bus" if bmi else "alternate_i2c", "address": 104 if bmi else 106,
               "wake": {"kind": "periodic"}}
        resources = {"receiver_uart": {"kind": "uart", "device": "/dev/ttyS1", "baud": 38400},
                     "sensor_bus": {"kind": "i2c", "device": "/dev/i2c0", "frequency": 400000}}
        if not bmi:
            resources["alternate_i2c"] = {"kind": "i2c", "device": "/dev/i2c1", "frequency": 100000}
        graph = {"version": 1, "mode": "hardware", "devices": {
            "imu": imu, "navigation": {"compatible": "u-blox,neo-m9n", "bus": "receiver_uart"}},
            "resources": resources, "aliases": {"imu": "imu", "gnss": "navigation"},
            "stack_bytes": 16384, "gnss_stack_bytes": 16384,
            "imu_implementation": "Bmi270" if bmi else "Lsm6dso", "gnss_implementation": "NeoM9n"}
        if matrix:
            graph["devices"]["secondary"] = {
                "compatible": "bosch,bmi270" if case == "two-bmi270" else "st,lsm6dso",
                "bus": "sensor_bus", "address": 105 if case == "two-bmi270" else 106,
                "wake": {"kind": "periodic"}}
            graph["aliases"].update(imu_primary_alias="imu", imu_secondary="secondary", gnss_primary_alias="navigation")
        data = {"board": "esp32s3-m5-cores3:nsh", "target": TARGET, "build": {"size-optimized": True},
                "resolved-nuttx-resources": {"fixture_bus": imu["bus"]}}
        semantic = CHECK.json_digest({"graph": graph, "target": TARGET,
                                      "nuttx-resources": data["resolved-nuttx-resources"]})
        overlay = [] if bmi else ["tests/host/fixtures/lsm6dso-overlay.toml"]
        if matrix:
            overlay = ["tests/nuttx-std/device-composition/overlays/two-bmi270.toml"]
            if case != "two-bmi270":
                overlay.append("tests/nuttx-std/device-composition/overlays/mixed-imu.toml")
        source_names = [name for name in self.sources if name != "app/event-demo/Cargo.toml"] + [app_path + "/Cargo.toml"]
        dependencies = [self.source_hash(name) for name in source_names + overlay]
        manifest = str(RECORDED / app_path / "Cargo.toml")
        receipt = {"version": 1, "workspace": str(RECORDED), "profile": "m5stack-cores3-gnss",
                   "target": TARGET, "os": "nuttx", "cli_overlays": overlay,
                   "product_manifest": manifest, "data": data, "graph": graph, "semantic_digest": semantic,
                   "raw_fingerprint": sha(case.encode()), "dependencies": dependencies}
        write_json(out / "resolved-profile.json", receipt)
        sources = {app_package: app_path, "nxrs-composition": "platform", "nxrs-acquisition": "hal/acquisition"}
        if "imu" in caps:
            sources.update({"nxrs-imu": "hal/imu", "nxrs-i2c": "driver/i2c", "nxrs-gpio": "driver/gpio"})
        if "gnss" in caps:
            sources.update({"nxrs-gnss": "hal/gnss", "nxrs-ubx-driver": "driver/ubx"})
        sources.update({"nxrs-" + driver: "driver/" + driver for driver in drivers})
        packages = {name: f"{name} v0.1.0 ({RECORDED / source})" for name, source in sources.items()}
        artifacts = [self.artifact(case, name, RECORDED / source) for name, source in sources.items()]
        app = next(a for a in artifacts if app_package + "@" in a["package_id"])
        app["target"].update(name=app_bin, kind=["bin"], crate_types=["bin"])
        executable = str(old / "cargo" / TARGET / "release" / app_bin)
        app.update(executable=executable, filenames=[executable])
        composition = next(a for a in artifacts if "nxrs-composition@" in a["package_id"])
        composition["target"]["name"] = "composition"
        features = drivers | ({"i2c"} if "imu" in caps else set()) | ({"ubx"} if "gnss" in caps else set())
        composition["features"] = sorted(features | caps)
        artifacts.append(self.artifact(case, "nxrs-build-support", RECORDED / "tools/build-support", "host"))
        artifacts.append(self.artifact(case, "nxrs-composition", RECORDED / "platform", "host", "custom-build"))
        std_root = old / "toolchain/lib/rustlib/src/rust/library/std"
        std = self.artifact(case, "std", std_root)
        std["package_id"] = std_root.as_uri().replace("file:", "path+file:", 1) + "#0.0.0"
        std["target"].update(name="std", kind=["rlib"], crate_types=["rlib"])
        artifacts.append(std)
        copied = old / "cargo" / TARGET / "release/build/nxrs-composition-f1/out"
        self.write_records(case, [*artifacts,
            {"reason": "build-script-executed", "package_id": composition["package_id"], "out_dir": str(copied)},
            {"reason": "build-finished", "success": True}])
        declarations = {
            "imu": ('declare_device!(PRIMARY_DEVICE, "imu", ::r#bmi270::Bmi270<resources::Bus, resources::Delay>, '
                    'resources::I2cResource, resources::open_i2c, (resource::Config { path: "/dev/i2c0", '
                    'frequency: 400000 }, 104), None, 16384, true);') if bmi else (
                    'declare_device!(PRIMARY_DEVICE, "imu", ::r#lsm6dso::Lsm6dso<resources::Bus>, '
                    'resources::I2cResource, resources::open_i2c, (resource::Config { path: "/dev/i2c1", '
                    'frequency: 100000 }, 106), None, 16384, true);'),
            "gnss": 'declare_device!(PRIMARY_DEVICE, "navigation", ::r#ubx::NeoM9n<Serial>, '
                    '"/dev/ttyS1", 38400, 16384, false);'}
        accessors = {"imu": {"imu": "PRIMARY_DEVICE", "primary_imu": "PRIMARY_DEVICE"},
                     "gnss": {"navigation": "PRIMARY_DEVICE", "primary_gnss": "PRIMARY_DEVICE"}}
        if matrix:
            secondary = declarations["imu"].replace('PRIMARY_DEVICE, "imu"', 'DEVICE_1, "secondary"')
            if case == "two-bmi270":
                secondary = secondary.replace("104)", "105)")
            else:
                secondary = secondary.replace("bmi270::Bmi270<resources::Bus, resources::Delay>", "lsm6dso::Lsm6dso<resources::Bus>").replace("104)", "106)")
            declarations["imu"] += "\n" + secondary
            accessors["imu"].update(primary_alias="PRIMARY_DEVICE", secondary="DEVICE_1")
            accessors["gnss"]["primary_alias"] = "PRIMARY_DEVICE"
        prepared = {}
        for cap in sorted(caps):
            identity = sha((case + cap).encode())
            text = "// Generated canonical device instances.\n" + declarations[cap] + "\npub mod devices {\n"
            text += "".join(f"pub fn r#{name}() -> &'static impl super::Device {{ &super::{symbol} }}\n"
                            for name, symbol in sorted(accessors[cap].items())) + "}\n"
            self.put(case, f"configuration/{identity}/{cap}_config.rs", text)
            self.put(case, copied.relative_to(old) / f"{cap}_config.rs", text)
            prepared[cap] = {"identity": identity, "output": {"file": cap + "_config.rs", "sha256": sha(text.encode())}}
        handoff = json.dumps({str(RECORDED / "platform"): prepared}, sort_keys=True, separators=(",", ":"))
        handoff_name = "configuration/" + sha(handoff.encode()) + ".json"
        self.put(case, handoff_name, handoff)
        native = []
        native_packages = {"nxrs-acquisition"} | ({"nxrs-gpio", "nxrs-i2c"} if "imu" in caps else set())
        native_packages |= {"nxrs-gnss"} if "gnss" in caps else set()
        for name in sorted(native_packages):
            path = self.put(case, "native-resources/" + name + ".a", "archive " + name + case)
            native.append({"package": packages[name], "archive": str(old / path.relative_to(out)), "sha256": CHECK.digest(path)})
        catalog = {"package": "nxrs-composition", "manifest": self.source_hash("platform/Cargo.toml"),
                   "dependencies": [self.source_hash(f"driver/{driver}/Cargo.toml") for driver in ("bmi270", "lsm6dso", "ubx")]}
        binding_inputs = {cap: {**catalog, "bindings": [self.source_hash(f"driver/{driver}/bindings.toml") for driver in drivers]}
                          for cap, drivers in (("imu", ("bmi270", "lsm6dso")), ("gnss", ("ubx",)))}
        features = ["nxrs-composition/" + f for f in sorted(features)]
        selection = {"app_manifest": manifest, "app_package": app_package, "platform": "m5stack-cores3-gnss",
                     "target_os": "nuttx", "nuttx-build-mode": "flat", "configuration": str(old / handoff_name),
                     "providers": [{"capability": c, "implementation": "nuttx", "package": "nxrs-" + c} for c in ("gnss", "imu")],
                     "profiles": [self.source_hash(n) for n in ("platform/cores3/profile.toml", "platform/nuttx/resources/esp32s3-m5-cores3.toml", *overlay)],
                     "driver_features": features, "model_features": [], "native-resources": native,
                     "dependency_resolution": {"status": "resolved", "packages": sorted(packages.values()),
                                               "driver_features": features, "model_features": []},
                     "resolved-composition": {"graph": graph, "semantic-digest": semantic,
                                              "input-fingerprint": receipt["raw_fingerprint"], "binding-inputs": binding_inputs}}
        write_json(out / "provider-selection.json", selection)
        self.put(case, "resolved.config", "CONFIG_ARCH_BOARD_ESP32S3_M5_CORES3=y\n")
        write_json(out / (TARGET + ".json"), {"arch": "xtensa", "os": "nuttx", "target-pointer-width": "32"})
        provenance = {"nxrs": REVISION, "nxrs_dirty": "no", "nuttx": "b" * 40, "apps": "c" * 40,
                      "app_package": app_package, "app_bin": app_bin, "platform": "m5stack-cores3-gnss",
                      "target": TARGET, "board": "esp32s3-m5-cores3:nsh", "nuttx_build_mode": "flat",
                      "size_optimized": "1", "app_command": app_bin.replace("-", "_")}
        compiler = ["rustc 1.90.0-nightly (fixture)", "commit-hash: " + "d" * 40, "commit-date: 2025-09-16",
                    "host: x86_64-unknown-linux-gnu", "release: 1.90.0-nightly", "LLVM version: 20.1.1",
                    "xtensa-esp-elf-gcc (crosstool-NG esp-14.2.0_20241119) 14.2.0"]
        self.put(case, "provenance.txt", "\n".join([*(k + "=" + v for k, v in provenance.items()), *compiler]) + "\n")
        rust = self.put(case, "rust-input.elf", b"\x7fELF Rust fixture " + case.encode())
        final = self.put(case, "nuttx/nuttx", b"\x7fELF final fixture " + case.encode())
        self.put(case, "rust-partial.map", "discarded nxrs_lsm6dso input is not live symbol evidence")
        self.put(case, "nuttx/nuttx.map", "fixture map; never a driver inclusion oracle")
        self.elf_output[str(rust)] = elf_text("REL", 144 if bmi else 168)
        self.elf_output[str(final)] = elf_text("EXEC", 144 if bmi else 168)
        if matrix:
            for path in (rust, final):
                text = self.elf_output[str(path)].replace("event_demo_main", app_bin.replace("-", "_") + "_main")
                if "imu" in caps:
                    size = 144 if case == "two-bmi270" else 168
                    text = text.replace("3fc90200 48 OBJECT LOCAL DEFAULT 2 composition::gnss_factory::PRIMARY_DEVICE",
                                        f"3fc90200 {size} OBJECT LOCAL DEFAULT 2 composition::imu_factory::DEVICE_1")
                else:
                    text = text.replace("  1: 3fc90000 144 OBJECT LOCAL DEFAULT 2 composition::imu_factory::PRIMARY_DEVICE\n", "")
                    text = text.replace("i2c_resource_transfer", "gnss_serial_configure")
                    text += " 7: 42000040 16 FUNC GLOBAL DEFAULT 1 gnss_serial_restore\n"
                self.elf_output[str(path)] = text
        write_json(out / "link-reuse.json", {"reused": False, "artifact_sha256": CHECK.digest(rust)})
        write_json(out / "rust-symbol-pruning.json", {"after_sha256": CHECK.digest(rust), "removed_local_undefined": []})
        write_json(out / "rust-link.json", {"linker": str(RECORDED / "target/qemu-tools/gcc/bin/xtensa-esp32s3-elf-ld"),
                   "effective": ["input.o", "-o", str(old / "cargo" / TARGET / "release/deps/event_demo-a1"), "-r", "--gc-sections"]})
        write_json(out / "std-source.json", {"std_source": str(std_root / "src/lib.rs"), "expected_source": str(std_root / "src/lib.rs"),
                                             "selected_source_matches": True, "rustc": str(old / "toolchain/bin/rustc")})
        self.put(case, "downloads.sha256", CHECK.digest(self.root / "target/qemu-tools/gcc.tar.xz")
                 + "  " + str(RECORDED / "target/qemu-tools/gcc.tar.xz") + "\n")
        hashes = {item["path"]: item["sha256"] for item in dependencies}
        hashes[executable] = CHECK.digest(rust)
        for name in ("provider-selection.json", "resolved.config", TARGET + ".json", "nuttx/nuttx"):
            hashes[str(old / name)] = CHECK.digest(out / name)
        self.put(case, "binaries.sha256", "".join(value + "  " + path + "\n" for path, value in hashes.items()))

    def records(self, case):
        return [json.loads(line) for line in (self.outputs[case] / "cargo-messages.jsonl").read_text().splitlines()]

    def write_records(self, case, records):
        self.put(case, "cargo-messages.jsonl", "\n".join(json.dumps(r) for r in records) + "\n")

    def refresh_hash(self, case, name):
        hashes = CHECK.hash_list((self.outputs[case] / "binaries.sha256").read_text())
        hashes[str(self.recorded_out(case) / name)] = CHECK.digest(self.outputs[case] / name)
        self.put(case, "binaries.sha256", "".join(h + "  " + p + "\n" for p, h in hashes.items()))

    def binutils(self, args):
        if "--version" in args:
            return "GNU " + Path(args[0]).name + " fixture\n"
        if Path(args[0]).name.endswith("readelf"):
            return self.elf_output[str(args[-1])]
        return "\n".join("42000000 T " + name for name in ("i2c_resource_transfer", "main", "event_demo_main", "nx_start",
            "device_composition_imu_main", "device_composition_gnss_main", "gnss_serial_configure", "gnss_serial_restore"))

    def git(self, args, cwd=None):
        if args[1:] == ["rev-parse", "HEAD"]:
            return REVISION + "\n"
        if args[1] == "status":
            return ""
        return ("b" if args[-1].endswith("/nuttx") else "c") * 40 + "\n"

    def check(self):
        with patch.object(CHECK, "command", side_effect=self.git):
            return CHECK.check_pair(self.outputs["bmi270"], self.outputs["lsm6dso"], REVISION,
                                    root=self.root, run=self.binutils)


class DeviceClosureTests(DeviceClosureFixture):
    def test_complete_relocated_pair_counts_static_storage_without_driver_method_names(self):
        report = self.check()
        self.assertEqual(report["static_object_bytes_delta_lsm6dso_minus_bmi270"], 24)
        for case, result in report["cases"].items():
            self.assertEqual(result["final_elf"]["static_object_count"], 2)
            self.assertEqual(result["final_elf"]["named_driver_symbol_count"], 0)
            self.assertIn("nxrs-" + case, result["cargo_packages"]["target"])
            self.assertIn("nxrs-build-support", result["cargo_packages"]["host"])
            self.assertTrue(any("/out/imu_config.rs" in p for p in result["evidence_sha256"]))
            self.assertIn("provenance.txt", result["evidence_sha256"])

    def test_relative_and_absolute_overlay_spellings_resolve_to_the_same_input(self):
        path = self.outputs["lsm6dso"] / "resolved-profile.json"
        receipt = read_json(path)
        for overlay in ("./tests/host/fixtures/lsm6dso-overlay.toml",
                        str(RECORDED / "tests/host/fixtures/lsm6dso-overlay.toml")):
            with self.subTest(overlay=overlay):
                receipt["cli_overlays"] = [overlay]
                write_json(path, receipt)
                self.check()

    def test_wrong_duplicate_or_escaping_overlay_receipts_are_rejected(self):
        path = self.outputs["lsm6dso"] / "resolved-profile.json"
        receipt = read_json(path)
        expected = "tests/host/fixtures/lsm6dso-overlay.toml"
        other = self.root / "other/lsm6dso-overlay.toml"
        other.parent.mkdir()
        other.write_bytes((self.root / expected).read_bytes())
        for overlays in ([], [expected, expected], ["other/lsm6dso-overlay.toml"],
                         ["../outside/lsm6dso-overlay.toml"],
                         ["/outside/tests/host/fixtures/lsm6dso-overlay.toml"]):
            with self.subTest(overlays=overlays):
                receipt["cli_overlays"] = overlays
                write_json(path, receipt)
                with self.assertRaisesRegex(ValueError, "overlay"):
                    self.check()

    def test_overlay_receipt_requires_an_array_of_strings(self):
        for case in ("bmi270", "lsm6dso"):
            path = self.outputs[case] / "resolved-profile.json"
            receipt = read_json(path)
            original = receipt["cli_overlays"]
            for value in ("", {}, None, False, [None], [123], [False],
                          {"tests/host/fixtures/lsm6dso-overlay.toml": None}):
                with self.subTest(case=case, value=value):
                    receipt["cli_overlays"] = value
                    write_json(path, receipt)
                    with self.assertRaisesRegex(ValueError, "array of path strings"):
                        self.check()
            receipt["cli_overlays"] = original
            write_json(path, receipt)

    def test_unselected_host_library_and_shared_host_target_identity_are_not_target_contamination(self):
        records = self.records("bmi270")
        records[-1:-1] = [self.artifact("bmi270", "nxrs-lsm6dso", RECORDED / "driver/lsm6dso", "host"),
                          self.artifact("bmi270", "nxrs-bmi270", RECORDED / "driver/bmi270", "host")]
        self.write_records("bmi270", records)
        report = self.check()
        self.assertIn("nxrs-lsm6dso", report["cases"]["bmi270"]["cargo_packages"]["host"])
        self.assertNotIn("nxrs-lsm6dso", report["cases"]["bmi270"]["cargo_packages"]["target"])

    def test_host_only_selected_driver_cannot_satisfy_target_presence(self):
        records = self.records("bmi270")
        selected = next(a for a in records if "nxrs-bmi270@" in a.get("package_id", ""))
        selected["filenames"] = self.artifact("bmi270", "nxrs-bmi270", RECORDED / "driver/bmi270", "host")["filenames"]
        self.write_records("bmi270", records)
        with self.assertRaisesRegex(ValueError, "missing from target artifacts"):
            self.check()

    def test_unselected_target_driver_is_rejected(self):
        records = self.records("bmi270")
        records.insert(-1, self.artifact("bmi270", "nxrs-lsm6dso", RECORDED / "driver/lsm6dso"))
        self.write_records("bmi270", records)
        with self.assertRaisesRegex(ValueError, "target contamination"):
            self.check()

    def test_host_only_kind_cannot_masquerade_in_target_directory(self):
        records = self.records("bmi270")
        records.insert(-1, self.artifact("bmi270", "macro", RECORDED / "macro", kind="proc-macro"))
        self.write_records("bmi270", records)
        with self.assertRaisesRegex(ValueError, "host-only"):
            self.check()

    def test_failed_or_truncated_cargo_evidence_is_rejected(self):
        records = self.records("bmi270")
        for invalid in (records[:-1], [*records[:-1], {"reason": "build-finished", "success": False}], []):
            with self.subTest(last=invalid[-1:] or None):
                self.write_records("bmi270", invalid)
                with self.assertRaisesRegex(ValueError, "successful final Cargo"):
                    self.check()

    def test_wrong_model_with_consistent_hashes_is_rejected(self):
        out = self.outputs["bmi270"]
        receipt = read_json(out / "resolved-profile.json")
        selection = read_json(out / "provider-selection.json")
        receipt["graph"]["devices"]["imu"]["compatible"] = "st,lsm6dso"
        selection["resolved-composition"]["graph"] = receipt["graph"]
        semantic = CHECK.json_digest({"graph": receipt["graph"], "target": TARGET,
                                      "nuttx-resources": receipt["data"]["resolved-nuttx-resources"]})
        receipt["semantic_digest"] = selection["resolved-composition"]["semantic-digest"] = semantic
        write_json(out / "resolved-profile.json", receipt)
        write_json(out / "provider-selection.json", selection)
        self.refresh_hash("bmi270", "provider-selection.json")
        with self.assertRaisesRegex(ValueError, "wrong bmi270 device model"):
            self.check()

    def test_swapped_provider_receipt_or_cargo_case_is_rejected(self):
        for name in ("provider-selection.json", "resolved-profile.json", "cargo-messages.jsonl"):
            path = self.outputs["bmi270"] / name
            before = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes((self.outputs["lsm6dso"] / name).read_bytes())
                with self.assertRaises(ValueError):
                    self.check()
            path.write_bytes(before)

    def test_missing_evidence_and_modified_source_are_rejected(self):
        path = self.outputs["bmi270"] / "rust-input.elf"
        before = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing recorded input"):
            self.check()
        path.write_bytes(before)
        (self.root / "platform/cores3/profile.toml").write_text("changed profile")
        with self.assertRaisesRegex(ValueError, "stale or mismatched"):
            self.check()

    def test_altered_rust_and_swapped_final_elf_fail_success_hash_binding(self):
        for name in ("rust-input.elf", "nuttx/nuttx"):
            path = self.outputs["bmi270"] / name
            before = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes((self.outputs["lsm6dso"] / name).read_bytes())
                with self.assertRaisesRegex(ValueError, "stale or mismatched"):
                    self.check()
            path.write_bytes(before)

    def test_stale_provenance_and_wrong_expected_checkout_are_rejected(self):
        path = self.outputs["bmi270"] / "provenance.txt"
        path.write_text(path.read_text().replace(REVISION, "f" * 40))
        with self.assertRaisesRegex(ValueError, "provenance nxrs"):
            self.check()
        with patch.object(CHECK, "command", return_value="e" * 40):
            with self.assertRaisesRegex(ValueError, "checkout revision mismatch"):
                CHECK.check_pair(*self.outputs.values(), REVISION, root=self.root)

    def test_compiler_pair_mismatch_is_rejected(self):
        path = self.outputs["lsm6dso"] / "provenance.txt"
        path.write_text(path.read_text().replace("LLVM version: 20.1.1", "LLVM version: 20.1.2"))
        with self.assertRaisesRegex(ValueError, "different compiler provenance"):
            self.check()

    def test_stale_immutable_handoff_and_consumed_configuration_are_rejected(self):
        out = self.outputs["bmi270"]
        selection = read_json(out / "provider-selection.json")
        handoff = out / Path(selection["configuration"]).relative_to(self.recorded_out("bmi270"))
        before = handoff.read_bytes()
        handoff.write_bytes(before + b" ")
        with self.assertRaisesRegex(ValueError, "stale configuration handoff"):
            self.check()
        handoff.write_bytes(before)
        copied = out / "cargo" / TARGET / "release/build/nxrs-composition-f1/out/imu_config.rs"
        copied.write_text(copied.read_text().replace("::r#bmi270", "::r#lsm6dso"))
        with self.assertRaisesRegex(ValueError, "stale or mismatched"):
            self.check()

    def test_wrong_target_machine_and_missing_static_object_are_rejected(self):
        path = str(self.outputs["bmi270"] / "nuttx/nuttx")
        before = self.elf_output[path]
        for text in (before.replace("Tensilica Xtensa Processor", "Advanced Micro Devices X86-64"),
                     before.replace("composition::imu_factory::PRIMARY_DEVICE", "unrelated_static")):
            with self.subTest(text=text):
                self.elf_output[path] = text
                with self.assertRaises(ValueError):
                    self.check()

    def test_function_closure_zero_size_and_duplicate_symbols_cannot_be_counted_as_device_storage(self):
        path = str(self.outputs["bmi270"] / "nuttx/nuttx")
        before = self.elf_output[path]
        for text in (before.replace("144 OBJECT", "144 FUNC"), before.replace("144 OBJECT", "0 OBJECT"),
                     before.replace("::imu_factory::PRIMARY_DEVICE", "::imu_factory::PRIMARY_DEVICE::{{closure}}"),
                     before + " 7: 3fc90300 144 OBJECT LOCAL DEFAULT 2 composition::imu_factory::DEVICE_0\n"):
            with self.subTest(text=text):
                self.elf_output[path] = text
                with self.assertRaisesRegex(ValueError, "static data|linked primary device objects"):
                    self.check()

    def test_retained_forbidden_symbol_is_rejected_even_when_cargo_claims_exclusion(self):
        path = str(self.outputs["bmi270"] / "nuttx/nuttx")
        self.elf_output[path] += " 7: 42000040 16 FUNC LOCAL DEFAULT 1 nxrs_lsm6dso::sample\n"
        with self.assertRaisesRegex(ValueError, "unexpected retained driver"):
            self.check()

    def test_archive_pin_and_native_resource_bytes_are_verified(self):
        archive = self.root / "target/qemu-tools/gcc.tar.xz"
        before = archive.read_bytes()
        archive.write_bytes(b"different tools")
        with self.assertRaisesRegex(ValueError, "stale or mismatched"):
            self.check()
        archive.write_bytes(before)
        self.put("bmi270", "native-resources/nxrs-i2c.a", "wrong native code")
        with self.assertRaisesRegex(ValueError, "stale or mismatched"):
            self.check()

    def test_consumed_gnss_native_archive_cannot_be_omitted(self):
        path = self.outputs["bmi270"] / "provider-selection.json"
        selection = read_json(path)
        selection["native-resources"] = [
            resource for resource in selection["native-resources"]
            if not resource["package"].startswith("nxrs-gnss ")]
        write_json(path, selection)
        self.refresh_hash("bmi270", "provider-selection.json")
        with self.assertRaisesRegex(ValueError, "wrong native resource closure"):
            self.check()

    def test_failed_cli_removes_old_success_report_and_success_is_published(self):
        report = self.root / "device-closure.json"
        argv = ["--bmi270-out", str(self.outputs["bmi270"]), "--lsm6dso-out", str(self.outputs["lsm6dso"]),
                "--revision", REVISION, "--report", str(report)]
        report.write_text('{"stale":true}')
        with patch.object(CHECK, "check_pair", side_effect=ValueError("bad evidence")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CHECK.main(argv), 1)
        self.assertFalse(report.exists())
        with patch.object(CHECK, "check_pair", return_value={"verified": True}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CHECK.main(argv), 0)
        self.assertEqual(read_json(report), {"verified": True})
        self.assertFalse(list(report.parent.glob(report.name + ".*.tmp")))


class DeviceMatrixTests(DeviceClosureFixture):
    def setUp(self):
        super().setUp()
        for name in ("imu/Cargo.toml", "gnss/Cargo.toml", "overlays/two-bmi270.toml", "overlays/mixed-imu.toml"):
            path = self.root / "tests/nuttx-std/device-composition" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture source: " + name)
        for case in ("two-bmi270", "mixed-imu", "gnss-only"):
            self.outputs[case] = self.root / "builds" / case
            self.make_case(case)

    def check(self):
        with patch.object(CHECK, "command", side_effect=self.git):
            return CHECK.check_matrix(*(self.outputs[case] for case in ("two-bmi270", "mixed-imu", "gnss-only")),
                                      REVISION, root=self.root, run=self.binutils)

    def rewrite_selection(self, case, selection):
        write_json(self.outputs[case] / "provider-selection.json", selection)
        self.refresh_hash(case, "provider-selection.json")

    def rewrite_handoff(self, case, change):
        out, old = self.outputs[case], self.recorded_out(case)
        selection = read_json(out / "provider-selection.json")
        selected = read_json(out / Path(selection["configuration"]).relative_to(old))
        change(selected[str(RECORDED / "platform")])
        text = json.dumps(selected, sort_keys=True, separators=(",", ":"))
        name = "configuration/" + sha(text.encode()) + ".json"
        self.put(case, name, text)
        selection["configuration"] = str(old / name)
        self.rewrite_selection(case, selection)

    def test_matrix_proves_declared_devices_and_exact_consumed_storage(self):
        report = self.check()
        self.assertEqual(set(report["cases"]), {"two-bmi270", "mixed-imu", "gnss-only"})
        for case, count, total, caps, native in (
            ("two-bmi270", 2, 288, ["imu"], ["nxrs-acquisition", "nxrs-gpio", "nxrs-i2c"]),
            ("mixed-imu", 2, 312, ["imu"], ["nxrs-acquisition", "nxrs-gpio", "nxrs-i2c"]),
            ("gnss-only", 1, 48, ["gnss"], ["nxrs-acquisition", "nxrs-gnss"]),
        ):
            with self.subTest(case=case):
                result = report["cases"][case]
                self.assertEqual(result["declared_device_count"], 3)
                self.assertEqual(result["consumed_device_count"], count)
                self.assertEqual(result["consumed_capabilities"], caps)
                self.assertEqual(result["native_resources"], native)
                self.assertEqual(result["final_elf"]["static_object_count"], count)
                self.assertEqual(result["final_elf"]["static_object_bytes"], total)
                self.assertEqual(result["final_elf"]["named_driver_symbol_count"], 0)
                for capability in {"imu", "gnss"} - set(caps):
                    self.assertNotIn("nxrs-" + capability, result["cargo_packages"]["target"])
        same, mixed, gnss = (report["cases"][name] for name in ("two-bmi270", "mixed-imu", "gnss-only"))
        self.assertNotIn("nxrs-lsm6dso", same["cargo_packages"]["target"])
        self.assertIn("nxrs-lsm6dso", mixed["cargo_packages"]["target"])
        self.assertEqual(gnss["driver_features"], ["nxrs-composition/ubx"])
        self.assertEqual(mixed["semantic_digest"], gnss["semantic_digest"])

    def test_second_selected_driver_must_be_a_target_library(self):
        records = self.records("mixed-imu")
        selected = next(r for r in records if "nxrs-lsm6dso@" in r.get("package_id", ""))
        selected["filenames"] = self.artifact("mixed-imu", "nxrs-lsm6dso", RECORDED / "driver/lsm6dso", "host")["filenames"]
        self.write_records("mixed-imu", records)
        with self.assertRaisesRegex(ValueError, "missing from target artifacts"):
            self.check()

    def test_declared_unconsumed_libraries_are_allowed_only_on_host(self):
        for case, package, source in (("two-bmi270", "nxrs-gnss", "hal/gnss"),
                                      ("gnss-only", "nxrs-bmi270", "driver/bmi270")):
            records = self.records(case)
            records.insert(-1, self.artifact(case, package, RECORDED / source, "host"))
            self.write_records(case, records)
            self.check()
            records[-2] = self.artifact(case, package, RECORDED / source)
            self.write_records(case, records)
            with self.assertRaisesRegex(ValueError, "target contamination"):
                self.check()
            self.write_records(case, records[:-2] + records[-1:])

    def test_declared_unconsumed_packages_cannot_enter_the_normal_graph(self):
        for case, package, source in (("two-bmi270", "nxrs-ubx-driver", "driver/ubx"),
                                      ("gnss-only", "nxrs-i2c", "driver/i2c")):
            path = self.outputs[case] / "provider-selection.json"
            original = read_json(path)
            selection = read_json(path)
            selection["dependency_resolution"]["packages"].append(f"{package} v0.1.0 ({RECORDED / source})")
            self.rewrite_selection(case, selection)
            with self.assertRaisesRegex(ValueError, "wrong selected/unselected driver or resource"):
                self.check()
            self.rewrite_selection(case, original)

    def test_unconsumed_capability_cannot_activate_features_or_preparation(self):
        case = "gnss-only"
        path = self.outputs[case] / "provider-selection.json"
        original = read_json(path)
        selection = read_json(path)
        for value in (selection, selection["dependency_resolution"]):
            value["driver_features"].append("nxrs-composition/i2c")
        self.rewrite_selection(case, selection)
        with self.assertRaisesRegex(ValueError, "derived driver/resource features"):
            self.check()
        self.rewrite_selection(case, original)
        self.rewrite_handoff(case, lambda selected: selected.update(imu=selected["gnss"]))
        with self.assertRaisesRegex(ValueError, "prepared catalogue/capabilities"):
            self.check()

    def test_extra_or_missing_native_helper_is_rejected(self):
        case = "gnss-only"
        selection = read_json(self.outputs[case] / "provider-selection.json")
        removed = selection["native-resources"].pop()
        self.rewrite_selection(case, selection)
        with self.assertRaisesRegex(ValueError, "native resource closure"):
            self.check()
        selection["native-resources"].append(removed)
        description = f"fixture-extra-native v0.1.0 ({RECORDED / 'fixture-extra-native'})"
        selection["dependency_resolution"]["packages"].append(description)
        path = self.put(case, "native-resources/extra.a", "unconsumed native helper")
        selection["native-resources"].append({"package": description, "archive": str(self.recorded_out(case) / path.relative_to(self.outputs[case])),
                                               "sha256": CHECK.digest(path)})
        self.rewrite_selection(case, selection)
        with self.assertRaisesRegex(ValueError, "native resource closure"):
            self.check()

    def test_alias_redirect_with_consistent_preparation_hashes_is_rejected(self):
        case = "mixed-imu"
        def redirect(selected):
            prepared = selected["imu"]
            name = "configuration/" + prepared["identity"] + "/imu_config.rs"
            text = (self.outputs[case] / name).read_text().replace(
                "r#primary_alias() -> &'static impl super::Device { &super::PRIMARY_DEVICE }",
                "r#primary_alias() -> &'static impl super::Device { &super::DEVICE_1 }")
            self.put(case, name, text)
            self.put(case, "cargo/" + TARGET + "/release/build/nxrs-composition-f1/out/imu_config.rs", text)
            prepared["output"]["sha256"] = sha(text.encode())
        self.rewrite_handoff(case, redirect)
        with self.assertRaisesRegex(ValueError, "canonical aliases"):
            self.check()

    def test_missing_second_or_extra_unconsumed_object_is_rejected(self):
        path = str(self.outputs["mixed-imu"] / "nuttx/nuttx")
        before = self.elf_output[path]
        for text in (before.replace("::imu_factory::DEVICE_1", "::unrelated::DEVICE_1"),
                     before + " 8: 3fc90300 48 OBJECT LOCAL DEFAULT 2 composition::gnss_factory::PRIMARY_DEVICE\n"):
            self.elf_output[path] = text
            with self.assertRaisesRegex(ValueError, "linked primary device objects"):
                self.check()

    def test_retained_gnss_helpers_match_the_consumed_capability(self):
        path = str(self.outputs["gnss-only"] / "nuttx/nuttx")
        before = self.elf_output[path]
        for helper in ("gnss_serial_configure", "gnss_serial_restore"):
            self.elf_output[path] = before.replace(helper, "unrelated_helper")
            with self.assertRaisesRegex(ValueError, "missing retained native helper"):
                self.check()
        self.elf_output[path] = before
        path = str(self.outputs["two-bmi270"] / "nuttx/nuttx")
        self.elf_output[path] += " 8: 42000040 16 FUNC GLOBAL DEFAULT 1 gnss_serial_configure\n"
        with self.assertRaisesRegex(ValueError, "unexpected retained driver"):
            self.check()

    def test_per_instance_sizes_cannot_be_swapped_between_rust_and_final_elf(self):
        path = str(self.outputs["mixed-imu"] / "nuttx/nuttx")
        self.elf_output[path] = self.elf_output[path].replace("PRIMARY_DEVICE", "SWAP").replace("DEVICE_1", "PRIMARY_DEVICE").replace("SWAP", "DEVICE_1")
        with self.assertRaisesRegex(ValueError, "Rust and final linked device storage differ"):
            self.check()

    def test_final_objects_must_not_overlap_or_exceed_their_section(self):
        path = str(self.outputs["mixed-imu"] / "nuttx/nuttx")
        before = self.elf_output[path]
        for text, message in ((before.replace("3fc90200 168", "3fc90040 168"), "overlapping"),
                              (before.replace("3fc90200 168", "3fc90400 168"), "outside its allocated section"),
                              (before.replace("3fc90200 168", "3fc90200 2048"), "outside its allocated section")):
            with self.subTest(message=message):
                self.elf_output[path] = text
                with self.assertRaisesRegex(ValueError, message):
                    self.check()

    def test_relocatable_device_sections_may_share_zero_offset(self):
        path = str(self.outputs["mixed-imu"] / "rust-input.elf")
        text = self.elf_output[path].replace(".data PROGBITS 3fc90000", ".data PROGBITS 00000000")
        text = text.replace("Symbol table", "  [ 3] .data.secondary PROGBITS 00000000 000600 000400 00 WA 0 0 4\nSymbol table")
        text = text.replace("3fc90000 144 OBJECT", "00000000 144 OBJECT")
        self.elf_output[path] = text.replace("3fc90200 168 OBJECT LOCAL DEFAULT 2", "00000000 168 OBJECT LOCAL DEFAULT 3")
        self.check()

    def test_same_concrete_type_has_equal_storage_across_instances_and_cases(self):
        for name in ("rust-input.elf", "nuttx/nuttx"):
            path = str(self.outputs["two-bmi270"] / name)
            self.elf_output[path] = self.elf_output[path].replace("3fc90200 144", "3fc90200 168")
        with self.assertRaisesRegex(ValueError, "same concrete BMI270"):
            self.check()

    def test_matrix_rejects_overlay_reordering_and_case_substitution(self):
        path = self.outputs["mixed-imu"] / "resolved-profile.json"
        receipt = read_json(path)
        receipt["cli_overlays"].reverse()
        write_json(path, receipt)
        with self.assertRaisesRegex(ValueError, "overlay receipt"):
            self.check()
        receipt["cli_overlays"].reverse()
        write_json(path, receipt)
        self.outputs["two-bmi270"], self.outputs["mixed-imu"] = self.outputs["mixed-imu"], self.outputs["two-bmi270"]
        with self.assertRaisesRegex(ValueError, "wrong two-bmi270 device model"):
            self.check()

    def test_matrix_does_not_admit_other_source_input_differences(self):
        case = "gnss-only"
        for name, message in (("tests/nuttx-std/device-composition/imu/Cargo.toml", "wrong fixture manifest"),
                              ("unexpected.toml", "differ beyond")):
            with self.subTest(name=name):
                source = self.root / name
                if not source.exists():
                    source.write_text("unapproved source")
                path = self.outputs[case] / "resolved-profile.json"
                before = path.read_bytes()
                receipt = read_json(path)
                receipt["dependencies"].append(self.source_hash(name))
                write_json(path, receipt)
                hashes = self.outputs[case] / "binaries.sha256"
                prior_hashes = hashes.read_text()
                hashes.write_text(prior_hashes + CHECK.digest(source) + "  " + str(RECORDED / name) + "\n")
                with self.assertRaisesRegex(ValueError, message):
                    self.check()
                path.write_bytes(before)
                hashes.write_text(prior_hashes)

    def test_matrix_verifies_common_compiler_and_binds_final_elf_hash(self):
        path = self.outputs["gnss-only"] / "provenance.txt"
        before = path.read_text()
        path.write_text(before.replace("LLVM version: 20.1.1", "LLVM version: 20.1.2"))
        with self.assertRaisesRegex(ValueError, "different compiler provenance"):
            self.check()
        path.write_text(before)
        (self.outputs["gnss-only"] / "nuttx/nuttx").write_bytes((self.outputs["mixed-imu"] / "nuttx/nuttx").read_bytes())
        with self.assertRaisesRegex(ValueError, "stale or mismatched"):
            self.check()

    def test_matrix_cli_requires_exact_complete_group_and_publishes_only_success(self):
        report = self.root / "device-matrix.json"
        common = ["--revision", REVISION, "--report", str(report)]
        trio = ["--same-model-out", str(self.outputs["two-bmi270"]), "--mixed-out", str(self.outputs["mixed-imu"]),
                "--gnss-only-out", str(self.outputs["gnss-only"])]
        for options in ([], trio[:2], trio[:4], [*trio, "--bmi270-out", str(self.outputs["bmi270"])]):
            report.write_text('{"stale":true}')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(CHECK.main(common + options), 1)
            self.assertFalse(report.exists())
        with patch.object(CHECK, "check_matrix", return_value={"matrix_verified": True}) as check, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CHECK.main(common + trio), 0)
            check.assert_called_once_with(self.outputs["two-bmi270"], self.outputs["mixed-imu"], self.outputs["gnss-only"], REVISION)
        self.assertEqual(read_json(report), {"matrix_verified": True})
        self.assertFalse(list(report.parent.glob(report.name + ".*.tmp")))


@unittest.skipUnless(all(shutil.which(tool) for tool in ("cc", "readelf", "nm")), "host C/binutils unavailable")
class RealElfParserTests(unittest.TestCase):
    def test_real_host_relocatable_object_sizes_and_native_symbols(self):
        # Actual ELF section/symbol parsing, explicitly admitted as host-only.
        # This neither manufactures a Xtensa header nor qualifies target builds.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assembly = root / "fixture.s"
            assembly.write_text('''.data
.globl "composition::imu_factory::PRIMARY_DEVICE"
.type "composition::imu_factory::PRIMARY_DEVICE", @object
.size "composition::imu_factory::PRIMARY_DEVICE", 144
"composition::imu_factory::PRIMARY_DEVICE":
.zero 144
.globl "composition::gnss_factory::PRIMARY_DEVICE"
.type "composition::gnss_factory::PRIMARY_DEVICE", @object
.size "composition::gnss_factory::PRIMARY_DEVICE", 48
"composition::gnss_factory::PRIMARY_DEVICE":
.zero 48
.text
.globl i2c_resource_transfer
.type i2c_resource_transfer, @function
i2c_resource_transfer:
.byte 0
.size i2c_resource_transfer, 1
.globl main
.type main, @function
main:
.byte 0
.size main, 1
''')
            binary = root / "fixture.o"
            subprocess.run(["cc", "-c", str(assembly), "-o", str(binary)], check=True, capture_output=True)
            header = subprocess.check_output(["readelf", "-h", str(binary)], text=True)
            machine = next(line.split(":", 1)[1].strip() for line in header.splitlines() if "Machine:" in line)
            elf_class = next(line.split(":", 1)[1].strip() for line in header.splitlines() if "Class:" in line)
            report = CHECK.inspect_elf(binary, {"readelf": Path(shutil.which("readelf")), "nm": Path(shutil.which("nm"))},
                                       "REL", "bmi270", machine=machine, elf_class=elf_class)
            self.assertEqual(report["static_object_count"], 2)
            self.assertEqual(report["static_object_bytes"], 192)
            self.assertEqual({s["section"] for s in report["static_objects"]}, {".data"})


if __name__ == "__main__":
    unittest.main()
