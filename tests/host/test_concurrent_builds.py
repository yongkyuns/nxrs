"""Independent negative controls; these synthetic files are never target evidence."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("concurrent_checker", ROOT / "tools/check-concurrent-builds.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
TARGET = "xtensa-esp32s3-nuttx"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else value.encode())
    return path


def save(path, value):
    return write(path, json.dumps(value, sort_keys=True, separators=(",", ":")))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def elf(kind):
    header = bytearray(52)
    header[:7] = b"\x7fELF\x01\x01\x01"
    struct.pack_into("<HH", header, 16, kind, 94)
    return bytes(header) + b"synthetic test data, not a compiled image\n"


class Witness:
    """Small internally consistent fixture with independent fixed device values."""
    def __init__(self, directory):
        self.root = directory / "source"
        self.root.mkdir()
        self.parent = self.root / "target/witness"
        for name in CHECK.SOURCE_INPUTS:
            write(self.root / name, f"synthetic tracked input {name}\n")
        write(self.root / ".gitignore", "/target/\n")
        write(self.root / "app/event-demo/Cargo.toml", "fixture app manifest\n")
        write(self.root / "app/event-demo/src/main.rs", "// fixture only\n")
        for name in CHECK.NATIVE.values():
            write(self.root / name / "Cargo.toml", f"fixture native package {name}\n")
            write(self.root / name / "src/platform/nuttx/ffi/native.c", "/* fixture native input */\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Concurrent checker fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic source inputs")
        revision = self.git("rev-parse", "HEAD")
        frontend_path = write(self.parent / "frontend/debug/nxrs-build", "fixture controller bytes\n")
        frontend_log = write(self.parent / "frontend-build.log", "fixture frontend log\n")
        frontend_messages = write(self.parent / "frontend-messages.jsonl", "\n".join(json.dumps(row) for row in [
            {"reason": "compiler-artifact", "package_id": f"path+{(self.root / 'tools/build').as_uri()}#nxrs-build@0.1.0",
             "target": {"kind": ["bin"], "src_path": str(self.root / "tools/build/src/main.rs")},
             "executable": str(frontend_path), "filenames": [str(frontend_path)], "fresh": False},
            {"reason": "build-finished", "success": True}]) + "\n")
        self.report = {
            "schema": 1, "clock": "monotonic_ns",
            "source": {"root": str(self.root), "revision": revision, "tree": self.git("rev-parse", "HEAD^{tree}"),
                       "inputs": {name: sha(self.root / name) for name in CHECK.SOURCE_INPUTS}},
            "frontend": {"path": str(frontend_path), "sha256": sha(frontend_path),
                "build_argv": ["cargo", "+1.90.0", "build", "--locked", "--manifest-path", str(self.root / "Cargo.toml"),
                               "-p", "nxrs-build", "--bin", "nxrs-build", "--message-format=json-render-diagnostics",
                               "--target-dir", str(self.parent / "frontend")],
                "compiler": {"path": "/immutable/rust/bin/rustc", "sha256": "1" * 64,
                             "version": "rustc 1.90.0 (fixture)\nhost: x86_64-unknown-linux-gnu"},
                "cargo_version": "cargo 1.90.0 (fixture)",
                "build_log": {"path": str(frontend_log), "sha256": sha(frontend_log)},
                "cargo_messages": {"path": str(frontend_messages), "sha256": sha(frontend_messages)}},
            "environment": {"CARGO_BUILD_JOBS": "2", "NXRS_NUTTX_BUILD_JOBS": "2"},
            "overlap": {"start_ns": 30, "end_ns": 40, "alive_before": ["bmi270", "lsm6dso"],
                        "alive_after": ["bmi270", "lsm6dso"]}, "cases": {}}
        for case, pid in (("bmi270", 11), ("lsm6dso", 12)):
            self.case(case, pid)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True,
                                       stderr=subprocess.STDOUT).strip()

    def case(self, case, pid):
        alternate = case == "lsm6dso"
        out = self.parent / case
        source = self.root
        compatible, chip, bus, device, frequency, address = (
            ("st,lsm6dso", "Lsm6dso", "alternate_i2c", "/dev/i2c1", 100000, 106) if alternate else
            ("bosch,bmi270", "Bmi270", "sensor_bus", "/dev/i2c0", 400000, 104))
        resources = {"receiver_uart": {"kind": "uart", "device": "/dev/ttyS1", "baud": 38400},
                     "sensor_bus": {"kind": "i2c", "device": "/dev/i2c0", "frequency": 400000}}
        if alternate:
            resources[bus] = {"kind": "i2c", "device": device, "frequency": frequency}
        graph = {"version": 1, "mode": "hardware", "resources": resources,
                 "devices": {"imu": {"compatible": compatible, "bus": bus, "address": address,
                                     "wake": {"kind": "periodic"}},
                             "navigation": {"compatible": "u-blox,neo-m9n", "bus": "receiver_uart"}},
                 "aliases": {"imu": "imu", "gnss": "navigation"}, "imu_implementation": chip,
                 "gnss_implementation": "NeoM9n", "stack_bytes": 16384, "gnss_stack_bytes": 16384}
        overlays = [str(source / "tests/host/fixtures/lsm6dso-overlay.toml")] if alternate else []
        profiles = [str(source / "platform/cores3/profile.toml"), *overlays,
                    str(source / "platform/nuttx/resources/esp32s3-m5-cores3.toml")]
        semantic = text_sha(json.dumps({"graph": graph, "target": TARGET, "nuttx-resources": None},
                                      sort_keys=True, separators=(",", ":")))
        save(out / ".nxrs-build.json", {"workspace": str(source)})
        save(out / "resolved-profile.json", {"version": 1, "workspace": str(source), "profile": "m5stack-cores3-gnss",
            "target": TARGET, "os": "nuttx", "cli_overlays": ["tests/host/fixtures/lsm6dso-overlay.toml"] if alternate else [],
            "product_manifest": str(source / "app/event-demo/Cargo.toml"), "graph": graph,
            "data": {"target": TARGET, "board": "esp32s3-m5-cores3:nsh"}, "semantic_digest": semantic,
            "raw_fingerprint": text_sha(case), "dependencies": [{"path": name, "sha256": sha(Path(name))}
                                                                   for name in profiles]})
        driver = "::r#lsm6dso::Lsm6dso<resources::Bus>" if alternate else "::r#bmi270::Bmi270<resources::Bus, resources::Delay>"
        generated = {
            "imu": "// Generated canonical device instances.\n"
                f'declare_device!(PRIMARY_DEVICE, "imu", {driver}, resources::I2cResource, resources::open_i2c, '
                f'(resource::Config {{ path: "{device}", frequency: {frequency} }}, {address}), None, 16384, true);\n'
                "pub mod devices {\npub fn r#imu() -> &'static impl super::Device { &super::PRIMARY_DEVICE }\n"
                "pub fn r#primary_imu() -> &'static impl super::Device { &super::PRIMARY_DEVICE }\n}\n",
            "gnss": '// Generated canonical device instances.\ndeclare_device!(PRIMARY_DEVICE, "navigation", '
                '::r#ubx::NeoM9n<Serial>, "/dev/ttyS1", 38400, 16384, false);\npub mod devices {\n'
                "pub fn r#navigation() -> &'static impl super::Device { &super::PRIMARY_DEVICE }\n"
                "pub fn r#primary_gnss() -> &'static impl super::Device { &super::PRIMARY_DEVICE }\n}\n"}
        configurations = {}
        for capability, contents in generated.items():
            identity = text_sha(case + capability)
            path = write(out / "configuration" / identity / f"{capability}_config.rs", contents)
            configurations[capability] = {"identity": identity, "output": {"file": path.name, "sha256": sha(path)}}
        handoff = json.dumps({str(source / "platform"): configurations}, sort_keys=True, separators=(",", ":"))
        configuration = write(out / "configuration" / (text_sha(handoff) + ".json"), handoff)
        native_values = {"I2C": "y", "I2C_DRIVER": "y", "ESP32S3_I2C0": "y", "ESP32S3_I2C0_MASTER_MODE": "y",
            "ESP32S3_I2C0_SCLPIN": "11", "ESP32S3_I2C0_SDAPIN": "12", "ESP32S3_UART1": "y",
            "ESP32S3_UART1_TXPIN": "17", "ESP32S3_UART1_RXPIN": "18", "UART1_BAUD": "38400",
            "ESP32S3_I2C1": "y" if alternate else None}
        if alternate:
            native_values.update(ESP32S3_I2C1_MASTER_MODE="y", ESP32S3_I2C1_SCLPIN="39", ESP32S3_I2C1_SDAPIN="38")
        header = write(out / "nuttx/include/nuttx/config.h", "".join(
            f"#define CONFIG_{key} {'1' if value == 'y' else value}\n" for key, value in native_values.items() if value is not None))
        write(out / "resolved.config", "".join(f"CONFIG_{key}={value}\n" if value is not None
            else f"# CONFIG_{key} is not set\n" for key, value in native_values.items()))
        context = save(out / "native-context.json", {"schema": 1, "target": TARGET,
            "directory": str(out / "apps/examples/nxrs_std_app"), "include": str(out / "nuttx/include"),
            "config_sha256": sha(header), "headers_sha256": "2" * 64,
            "cc": ["/immutable/xtensa/bin/xtensa-esp32s3-elf-gcc"],
            "ar": ["/immutable/xtensa/bin/xtensa-esp32s3-elf-ar", "rcs"],
            "cflags": ["-I", str(out / "nuttx/include")]})
        native_resources = []
        native_messages = []
        for owner, relative in CHECK.NATIVE.items():
            directory = source / relative
            bucket = text_sha(json.dumps(str(directory)))
            archive_bytes = f"!<arch>\nfixture shared immutable archive for {owner}\n".encode()
            archive_sha = hashlib.sha256(archive_bytes).hexdigest()
            archive = write(out / "native-resources" / bucket / archive_sha / f"lib{owner[5:]}_native.a", archive_bytes)
            ffi = directory / "src/platform/nuttx/ffi"
            inputs = {str(directory / "Cargo.toml"): sha(directory / "Cargo.toml"),
                      str(context): sha(context), str(header): sha(header),
                      str(ffi): text_sha(json.dumps({"native.c": sha(ffi / "native.c")}, separators=(",", ":"))),
                      "/immutable/xtensa/bin/xtensa-esp32s3-elf-gcc": "3" * 64,
                      "/immutable/xtensa/bin/xtensa-esp32s3-elf-ar": "4" * 64}
            save(archive.parent.parent / "inputs.json", {"identity": text_sha(case + owner), "tool_path": "5" * 64,
                                                        "archive": archive_sha, "inputs": inputs})
            native_resources.append({"package": f"{owner} v0.1.0 ({directory})", "sha256": archive_sha, "archive": str(archive)})
            native_messages.append({"reason": "build-script-executed", "package_id": f"path+{directory.as_uri()}#{owner}@0.1.0",
                "out_dir": str(out / "cargo" / TARGET / "release/build" / (owner + "-fixture") / "out"),
                "linked_paths": [f"native={archive.parent}"], "linked_libs": [f"static={owner[5:]}_native"], "env": []})
        save(out / "native-resources/packages.json", sorted(str(source / value) for value in CHECK.NATIVE.values()))
        packages = ["nxrs-" + case, "nxrs-composition", "nxrs-event-demo", "nxrs-imu", "nxrs-gnss", "nxrs-ubx-driver"]
        save(out / "provider-selection.json", {"app_manifest": str(source / "app/event-demo/Cargo.toml"),
            "app_package": "nxrs-event-demo", "platform": "m5stack-cores3-gnss", "target_os": "nuttx", "nuttx-build-mode": "flat",
            "profiles": [{"path": name, "sha256": sha(Path(name))} for name in profiles],
            "resolved-composition": {"graph": graph, "sources": profiles, "semantic-digest": semantic, "input-fingerprint": text_sha(case)},
            "dependency_resolution": {"status": "resolved", "packages": [f"{name} v0.1.0" for name in packages]},
            "configuration": str(configuration), "native-resources": native_resources})
        cargo_binary = write(out / "cargo" / TARGET / "release/event-demo", elf(1))
        messages = []
        library_sources = {"nxrs-" + case: "driver/" + case, "nxrs-composition": "platform",
                           "nxrs-imu": "hal/imu", "nxrs-gnss": "hal/gnss", "nxrs-ubx-driver": "driver/ubx"}
        for package in packages:
            main = package == "nxrs-event-demo"
            artifact = cargo_binary if main else write(cargo_binary.parent / "deps" / (package + ".rlib"),
                                                       b"!<arch>\nfixture target library\n")
            messages.append({"reason": "compiler-artifact", "package_id": f"path+{source.as_uri()}#{package}@0.1.0",
                "filenames": [str(artifact)],
                "fresh": False, "executable": str(cargo_binary) if main else None,
                "target": {"kind": ["bin"] if main else ["lib"], "src_path": str(source / "app/event-demo/src/main.rs")
                           if main else str(source / library_sources[package] / "src/lib.rs")}})
        composition_out = out / "cargo" / TARGET / "release/build/nxrs-composition-fixture/out"
        for capability, contents in generated.items():
            write(composition_out / f"{capability}_config.rs", contents)
            save(composition_out / f"nxrs-composition-{capability}.configuration.json", {"version": 1, "file": f"{capability}_config.rs"})
        messages += [{"reason": "build-script-executed", "package_id": f"path+{(source / 'platform').as_uri()}#nxrs-composition@0.1.0",
                      "out_dir": str(composition_out), "linked_paths": [], "linked_libs": [], "env": []},
                     *native_messages, {"reason": "build-finished", "success": True}]
        write(out / "cargo-messages.jsonl", "\n".join(json.dumps(row) for row in messages) + "\n")
        save(out / "std-source.json", {"selected_source_matches": True,
            "std_source": str(out / "toolchain/lib/rustlib/src/rust/library/std/src/lib.rs"),
            "expected_source": str(out / "toolchain/lib/rustlib/src/rust/library/std/src/lib.rs"), "rustc": str(out / "toolchain/bin/rustc")})
        save(out / "rust-link.json", {"original": ["-o", str(cargo_binary.parent / "deps/event_demo-fixture")],
            "effective": ["-o", str(cargo_binary.parent / "deps/event_demo-fixture"), "-r", "--gc-sections", "-u", "main", f"-Map={out / 'rust-partial.map'}"]})
        write(out / "rust-input.elf", cargo_binary.read_bytes())
        write(out / "rust-partial.map", "fixture link map\n")
        write(out / "nxrs-nuttx-link", (self.parent / "frontend/debug/nxrs-build").read_bytes())
        write(out / "nuttx/nuttx", elf(2))
        write(out / "nuttx/nuttx.bin", b"fixture image\n")
        write(out / "downloads.sha256", "fixture toolchain provenance\n")
        patch = write(out / "std-fixture.patch", "fixture private SDK patch\n")
        save(out / "std-patch.json", {"sdk": "esp-1.90.0.0", "installed_toolchain_unchanged": True,
                                      "patchset_sha256": {patch.name: sha(patch)}})
        for name in ("nuttx-patches.json", "nuttx-apps-patches.json", "esp-hal-patches.json"):
            save(out / name, {"fixture": True})
        write(out / "provenance.txt", f"nxrs={self.report['source']['revision']}\nnxrs_dirty=no\napp_package=nxrs-event-demo\n"
              "app_bin=event-demo\nplatform=m5stack-cores3-gnss\ntarget=xtensa-esp32s3-nuttx\nnuttx_build_mode=flat\nboard=esp32s3-m5-cores3:nsh\n")
        self.binary_paths = getattr(self, "binary_paths", {})
        self.binary_paths[case] = [out / name for name in ("nuttx/nuttx", "nuttx/nuttx.bin", f"cargo/{TARGET}/release/event-demo",
                                                         "provider-selection.json", "resolved.config")]
        self.binary_paths[case] += [source / "app/event-demo/Cargo.toml", *(Path(name) for name in profiles)]
        self.rehash_binaries(case)
        log = write(self.parent / f"{case}.log", "fixture normal build log\n")
        argv = [self.report["frontend"]["path"], "--workspace-root", str(source), "build", "--app", "event-demo", "--platform", "cores3"]
        if alternate:
            argv += ["--overlay", "tests/host/fixtures/lsm6dso-overlay.toml"]
        self.report["cases"][case] = {"output": str(out), "pid": pid, "argv": [*argv, "--out", str(out)],
            "started_ns": 10, "exited_ns": 50, "returncode": 0, "build_log": {"path": str(log), "sha256": sha(log)},
            "prepared_files": CHECK.snapshot_prepared(case, out, source),
            "artifacts": CHECK.finish_case(case, out, source, self.report["source"]["revision"])}

    def rehash_binaries(self, case):
        write(self.parent / case / "binaries.sha256", "".join(f"{sha(path)}  {path}\n" for path in self.binary_paths[case]))

    def json(self, case, name):
        return json.loads((self.parent / case / name).read_text())

    def validate(self, report=None, outputs=None):
        return CHECK.check_report(report or self.report, self.root, outputs)


class ConcurrentCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nxrs-concurrent-checker-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Witness(Path(self.temporary.name))
        self.out = self.fixture.parent / "bmi270"

    def test_complete_fixed_witness_and_identical_native_bytes_are_accepted(self):
        result = self.fixture.validate()
        self.assertEqual(result["status"], "passed")
        self.assertNotEqual(result["cases"]["bmi270"]["semantic_digest"], result["cases"]["lsm6dso"]["semantic_digest"])
        first = self.fixture.json("bmi270", "provider-selection.json")["native-resources"]
        second = self.fixture.json("lsm6dso", "provider-selection.json")["native-resources"]
        self.assertEqual([row["sha256"] for row in first], [row["sha256"] for row in second])

    def test_partial_and_initial_selection_do_not_signal_prepared(self):
        selection = self.out / "provider-selection.json"
        for contents in ('{"configuration":', '{"dependency_resolution":{"status":"pending"}}'):
            write(selection, contents)
            self.assertIsNone(CHECK.snapshot_prepared("bmi270", self.out, self.fixture.root))
        selection.unlink()
        self.assertIsNone(CHECK.snapshot_prepared("bmi270", self.out, self.fixture.root))

    def test_wrong_model_address_bus_or_overlay_is_rejected(self):
        path = self.out / "resolved-profile.json"
        original = json.loads(path.read_text())
        mutations = [lambda row: row["graph"]["devices"]["imu"].update(address=106),
                     lambda row: row["graph"]["devices"]["imu"].update(compatible="st,lsm6dso"),
                     lambda row: row["graph"]["resources"]["sensor_bus"].update(device="/dev/i2c1"),
                     lambda row: row.update(cli_overlays=[str(self.fixture.root / CHECK.OVERLAY)])]
        for change in mutations:
            with self.subTest(change=change):
                row = copy.deepcopy(original)
                change(row)
                save(path, row)
                with self.assertRaises(ValueError):
                    CHECK.snapshot_prepared("bmi270", self.out, self.fixture.root)

    def test_foreign_preparation_or_native_context_is_rejected(self):
        selection = self.fixture.json("bmi270", "provider-selection.json")
        selection["configuration"] = self.fixture.json("lsm6dso", "provider-selection.json")["configuration"]
        save(self.out / "provider-selection.json", selection)
        with self.assertRaisesRegex(ValueError, "escapes its own root"):
            CHECK.snapshot_prepared("bmi270", self.out, self.fixture.root)

    def test_native_inputs_cannot_be_owned_by_the_other_output(self):
        resource = self.fixture.json("bmi270", "provider-selection.json")["native-resources"][0]
        path = Path(resource["archive"]).parent.parent / "inputs.json"
        record = json.loads(path.read_text())
        record["inputs"][str(self.fixture.parent / "lsm6dso/nuttx/include/nuttx/config.h")] = "0" * 64
        save(path, record)
        with self.assertRaisesRegex(ValueError, "escapes its build/source/tool roots"):
            CHECK.snapshot_prepared("bmi270", self.out, self.fixture.root)

    def test_prepared_native_header_mutation_is_rejected(self):
        write(self.out / "nuttx/include/nuttx/config.h", "#define MUTATED 1\n")
        with self.assertRaisesRegex(ValueError, "changed prepared native input"):
            self.fixture.validate()

    def test_consistently_rehashed_native_config_transplant_is_rejected(self):
        alternate = self.fixture.parent / "lsm6dso"
        for name in ("resolved.config", "nuttx/include/nuttx/config.h"):
            write(alternate / name, (self.out / name).read_bytes())
        context_path = alternate / "native-context.json"
        context = json.loads(context_path.read_text())
        context["config_sha256"] = sha(alternate / "nuttx/include/nuttx/config.h")
        save(context_path, context)
        for path in (alternate / "native-resources").rglob("inputs.json"):
            record = json.loads(path.read_text())
            record["inputs"][str(context_path)] = sha(context_path)
            record["inputs"][str(alternate / "nuttx/include/nuttx/config.h")] = context["config_sha256"]
            save(path, record)
        self.fixture.rehash_binaries("lsm6dso")
        self.assertIsNotNone(CHECK.snapshot_prepared("lsm6dso", alternate, self.fixture.root))
        with self.assertRaisesRegex(ValueError, "native Kconfig does not implement the lsm6dso device contract"):
            CHECK.finish_case("lsm6dso", alternate, self.fixture.root, self.fixture.report["source"]["revision"])

    def test_native_header_disable_semantics_are_not_text_presence(self):
        header = self.out / "nuttx/include/nuttx/config.h"
        original = header.read_text()
        write(header, original + "#undef CONFIG_ESP32S3_I2C1\n")
        CHECK.check_native_configuration("bmi270", self.out)
        write(header, original + "#define CONFIG_ESP32S3_I2C1 0\n")
        with self.assertRaisesRegex(ValueError, "native header"):
            CHECK.check_native_configuration("bmi270", self.out)
        write(header, original + "#undef CONFIG_ESP32S3_I2C0\n")
        with self.assertRaisesRegex(ValueError, "duplicate native configuration directive"):
            CHECK.check_native_configuration("bmi270", self.out)

    def test_generated_source_cannot_be_replaced_even_with_updated_handoff_hash(self):
        selection = self.fixture.json("bmi270", "provider-selection.json")
        handoff_path = Path(selection["configuration"])
        handoff = json.loads(handoff_path.read_text())
        record = handoff[str(self.fixture.root / "platform")]["imu"]
        generated = handoff_path.parent / record["identity"] / record["output"]["file"]
        write(generated, generated.read_text().replace("104", "105"))
        record["output"]["sha256"] = sha(generated)
        temporary = save(handoff_path, handoff)
        renamed = handoff_path.with_name(sha(temporary) + ".json")
        temporary.rename(renamed)
        selection["configuration"] = str(renamed)
        save(self.out / "provider-selection.json", selection)
        with self.assertRaisesRegex(ValueError, "incorrect generated device construction"):
            CHECK.snapshot_prepared("bmi270", self.out, self.fixture.root)

    def test_content_stability_is_checked_against_observed_snapshot(self):
        path = self.out / "provider-selection.json"
        write(path, path.read_text() + "\n")
        self.fixture.rehash_binaries("bmi270")
        with self.assertRaisesRegex(ValueError, "prepared inputs changed"):
            self.fixture.validate()

    def test_cargo_driver_and_completion_negative_controls(self):
        path = self.out / "cargo-messages.jsonl"
        original = [json.loads(line) for line in path.read_text().splitlines()]
        for mutation in ("missing-driver", "wrong-driver", "host-only-driver", "failed-build", "fresh-app", "foreign-output", "missing-configuration", "wrong-native-link"):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(original)
                if mutation == "missing-driver":
                    rows = [row for row in rows if "nxrs-bmi270@" not in row.get("package_id", "")]
                elif mutation == "wrong-driver":
                    rows[0]["package_id"] = rows[0]["package_id"].replace("nxrs-bmi270", "nxrs-lsm6dso")
                elif mutation == "host-only-driver":
                    rows[0]["filenames"] = [str(self.out / "cargo/release/deps/nxrs_bmi270.rlib")]
                elif mutation == "failed-build":
                    rows[-1]["success"] = False
                elif mutation == "fresh-app":
                    next(row for row in rows if row.get("executable"))["fresh"] = True
                elif mutation == "foreign-output":
                    rows[0]["filenames"] = [str(self.fixture.parent / "lsm6dso/cargo" / TARGET / "release/driver.rlib")]
                elif mutation == "missing-configuration":
                    rows = [row for row in rows if not (row["reason"] == "build-script-executed" and "nxrs-composition@" in row["package_id"])]
                else:
                    next(row for row in rows if row.get("linked_libs"))["linked_paths"] = [f"native={self.out / 'native-resources/wrong'}"]
                write(path, "\n".join(json.dumps(row) for row in rows) + "\n")
                with self.assertRaises(ValueError):
                    CHECK.finish_case("bmi270", self.out, self.fixture.root, self.fixture.report["source"]["revision"])

    def test_private_sdk_and_final_elf_negative_controls(self):
        path = self.out / "std-source.json"
        original = json.loads(path.read_text())
        changed = dict(original, rustc=str(self.fixture.parent / "lsm6dso/toolchain/bin/rustc"))
        save(path, changed)
        with self.assertRaisesRegex(ValueError, "private Rust SDK"):
            self.fixture.validate()
        save(path, original)
        write(self.out / "nuttx/nuttx", elf(1))
        with self.assertRaisesRegex(ValueError, "wrong ELF"):
            self.fixture.validate()

    def test_linked_elf_must_equal_retained_rust_input(self):
        write(self.out / "rust-input.elf", elf(1) + b"changed")
        with self.assertRaisesRegex(ValueError, "differs from linked Cargo ELF"):
            self.fixture.validate()

    def test_provenance_must_bind_exact_clean_revision(self):
        path = self.out / "provenance.txt"
        write(path, path.read_text().replace(self.fixture.report["source"]["revision"], "0" * 40))
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.fixture.validate()

    def test_overlap_and_process_controls(self):
        mutations = [lambda row: row["overlap"].update(end_ns=30),
                     lambda row: row["overlap"].update(alive_after=["bmi270"]),
                     lambda row: row["cases"]["bmi270"].update(exited_ns=35),
                     lambda row: row["cases"]["lsm6dso"].update(started_ns=35),
                     lambda row: row["cases"]["lsm6dso"].update(pid=11),
                     lambda row: row["cases"]["bmi270"].update(returncode=1)]
        for change in mutations:
            with self.subTest(change=change):
                report = copy.deepcopy(self.fixture.report)
                change(report)
                with self.assertRaises(ValueError):
                    self.fixture.validate(report)

    def test_frontend_and_command_source_binding_controls(self):
        mutations = [lambda row: row["frontend"].update(sha256="0" * 64),
                     lambda row: row["frontend"]["compiler"].update(version="rustc 1.89.0"),
                     lambda row: row["frontend"].update(cargo_version="cargo 1.89.0"),
                     lambda row: row["frontend"]["build_argv"].remove("--locked"),
                     lambda row: row["cases"]["lsm6dso"]["argv"].remove("--overlay"),
                     lambda row: row["environment"].update(NXRS_SHARED_CARGO_TARGET_DIR="/shared")]
        for change in mutations:
            with self.subTest(change=change):
                report = copy.deepcopy(self.fixture.report)
                change(report)
                with self.assertRaises(ValueError):
                    self.fixture.validate(report)

    def test_foreign_frontend_compiler_artifact_is_rejected(self):
        record = self.fixture.report["frontend"]["cargo_messages"]
        path = Path(record["path"])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["target"]["src_path"] = "/foreign/tools/build/src/main.rs"
        write(path, "\n".join(json.dumps(row) for row in rows) + "\n")
        record["sha256"] = sha(path)
        with self.assertRaisesRegex(ValueError, "source-bound frontend"):
            self.fixture.validate()

    def test_stale_hash_inventory_and_linker_controller_are_rejected(self):
        report = copy.deepcopy(self.fixture.report)
        report["cases"]["bmi270"]["artifacts"].pop("rust-input.elf")
        with self.assertRaisesRegex(ValueError, "inventory/hash mismatch"):
            self.fixture.validate(report)
        write(self.out / "nxrs-nuttx-link", "foreign controller\n")
        self.fixture.report["cases"]["bmi270"]["artifacts"]["nxrs-nuttx-link"] = sha(self.out / "nxrs-nuttx-link")
        with self.assertRaisesRegex(ValueError, "controller differs"):
            self.fixture.validate()

    def test_source_checkout_and_input_hashes_must_match(self):
        report = copy.deepcopy(self.fixture.report)
        report["source"]["inputs"]["Cargo.lock"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source input changed"):
            self.fixture.validate(report)
        write(self.fixture.root / "Cargo.lock", "changed source\n")
        with self.assertRaisesRegex(ValueError, "not clean"):
            self.fixture.validate()

    def test_evidence_symlink_outside_case_is_rejected(self):
        path = self.out / "rust-input.elf"
        path.unlink()
        path.symlink_to(self.fixture.parent / "lsm6dso/rust-input.elf")
        with self.assertRaisesRegex(ValueError, "escaped evidence"):
            self.fixture.validate()

    def test_finite_bundle_replays_without_original_mutable_outputs(self):
        bundle = CHECK.export_bundle(self.fixture.report, self.fixture.root, self.fixture.parent / "evidence")
        self.assertTrue((bundle / "concurrent-builds.json").is_file())
        self.assertFalse((bundle / "bmi270/toolchain/bin/rustc").exists())
        for case in CHECK.CASES:
            shutil.rmtree(self.fixture.parent / case)
        result = self.fixture.validate(outputs={case: bundle / case for case in CHECK.CASES})
        self.assertEqual(result["status"], "passed")
        with self.assertRaisesRegex(ValueError, "bundle already exists"):
            # Use explicit validation above; a second publication can never
            # silently replace an already retained witness.
            CHECK.export_bundle(self.fixture.report, self.fixture.root, bundle)

    def test_broken_bundle_never_publishes_a_success_report(self):
        write(self.out / "rust-input.elf", b"broken")
        bundle = self.fixture.parent / "evidence"
        with self.assertRaises(ValueError):
            CHECK.export_bundle(self.fixture.report, self.fixture.root, bundle)
        self.assertFalse(bundle.exists())

    def test_cli_removes_stale_validation_on_failure(self):
        report = save(self.fixture.parent / "input.json", self.fixture.report)
        validated = write(self.fixture.parent / "validated.json", '{"status":"passed"}\n')
        write(self.out / "rust-input.elf", b"broken")
        result = subprocess.run([sys.executable, str(ROOT / "tools/check-concurrent-builds.py"),
            "--report", str(report), "--repo", str(self.fixture.root), "--validated-report", str(validated)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(validated.exists())


if __name__ == "__main__":
    unittest.main()
