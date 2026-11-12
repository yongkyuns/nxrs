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


class DeviceClosureTests(unittest.TestCase):
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
        return RECORDED / "target/firmware/event-demo" / case

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
        bmi = case == "bmi270"
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
        data = {"board": "esp32s3-m5-cores3:nsh", "target": TARGET, "build": {"size-optimized": True},
                "resolved-nuttx-resources": {"fixture_bus": imu["bus"]}}
        semantic = CHECK.json_digest({"graph": graph, "target": TARGET,
                                      "nuttx-resources": data["resolved-nuttx-resources"]})
        overlay = [] if bmi else ["tests/host/fixtures/lsm6dso-overlay.toml"]
        dependencies = [self.source_hash(name) for name in self.sources + overlay]
        manifest = str(RECORDED / "app/event-demo/Cargo.toml")
        receipt = {"version": 1, "workspace": str(RECORDED), "profile": "m5stack-cores3-gnss",
                   "target": TARGET, "os": "nuttx", "cli_overlays": [str(RECORDED / n) for n in overlay],
                   "product_manifest": manifest, "data": data, "graph": graph, "semantic_digest": semantic,
                   "raw_fingerprint": sha(case.encode()), "dependencies": dependencies}
        write_json(out / "resolved-profile.json", receipt)
        sources = {"nxrs-event-demo": "app/event-demo", "nxrs-composition": "platform",
                   "nxrs-imu": "hal/imu", "nxrs-gnss": "hal/gnss", "nxrs-i2c": "driver/i2c",
                   "nxrs-ubx-driver": "driver/ubx", "nxrs-acquisition": "hal/acquisition",
                   "nxrs-gpio": "driver/gpio", "nxrs-" + case: "driver/" + case}
        packages = {name: f"{name} v0.1.0 ({RECORDED / source})" for name, source in sources.items()}
        artifacts = [self.artifact(case, name, RECORDED / source) for name, source in sources.items()]
        app = next(a for a in artifacts if "nxrs-event-demo@" in a["package_id"])
        app["target"].update(name="event-demo", kind=["bin"], crate_types=["bin"])
        executable = str(old / "cargo" / TARGET / "release/event-demo")
        app.update(executable=executable, filenames=[executable])
        composition = next(a for a in artifacts if "nxrs-composition@" in a["package_id"])
        composition["target"]["name"] = "composition"
        composition["features"] = [case, "i2c", "ubx", "imu", "gnss"]
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
        prepared = {}
        for cap, declaration in declarations.items():
            identity = sha((case + cap).encode())
            text = "// Generated canonical device instances.\n" + declaration + "\n"
            self.put(case, f"configuration/{identity}/{cap}_config.rs", text)
            self.put(case, copied.relative_to(old) / f"{cap}_config.rs", text)
            prepared[cap] = {"identity": identity, "output": {"file": cap + "_config.rs", "sha256": sha(text.encode())}}
        handoff = json.dumps({str(RECORDED / "platform"): prepared}, sort_keys=True, separators=(",", ":"))
        handoff_name = "configuration/" + sha(handoff.encode()) + ".json"
        self.put(case, handoff_name, handoff)
        native = []
        for name in ("nxrs-acquisition", "nxrs-gnss", "nxrs-gpio", "nxrs-i2c"):
            path = self.put(case, "native-resources/" + name + ".a", "archive " + name + case)
            native.append({"package": packages[name], "archive": str(old / path.relative_to(out)), "sha256": CHECK.digest(path)})
        catalog = {"package": "nxrs-composition", "manifest": self.source_hash("platform/Cargo.toml"),
                   "dependencies": [self.source_hash(f"driver/{driver}/Cargo.toml") for driver in ("bmi270", "lsm6dso", "ubx")]}
        binding_inputs = {cap: {**catalog, "bindings": [self.source_hash(f"driver/{driver}/bindings.toml") for driver in drivers]}
                          for cap, drivers in (("imu", ("bmi270", "lsm6dso")), ("gnss", ("ubx",)))}
        features = ["nxrs-composition/" + f for f in (case, "i2c", "ubx")]
        selection = {"app_manifest": manifest, "app_package": "nxrs-event-demo", "platform": "m5stack-cores3-gnss",
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
                      "app_package": "nxrs-event-demo", "app_bin": "event-demo", "platform": "m5stack-cores3-gnss",
                      "target": TARGET, "board": "esp32s3-m5-cores3:nsh", "nuttx_build_mode": "flat",
                      "size_optimized": "1", "app_command": "event_demo"}
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
        return "\n".join("42000000 T " + name for name in ("i2c_resource_transfer", "main", "event_demo_main", "nx_start"))

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

