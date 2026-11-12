"""Reject partial evidence and host/native or capacity relabelling."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from report import parse, verify_target
import target_evidence


def baseline():
    case = dict(schema=1, domain="host-functional", case="baseline", transport="none",
                owners=0, workers=0, stack_request_per_worker=0, stack_request_total=0,
                queues=0, slots_per_queue=0, event_bytes=64, payload_capacity_bytes=0,
                native_heap_available=False, native_stack_available=False, workers_joined=0,
                valid=True, payload_valid=True, released_rust_bytes_delta=0, released_rust_blocks_delta=0)
    sample = dict(rust_live=64, rust_blocks=1, rust_peak_process=100,
                  alloc=2, zeroed=0, realloc=0, dealloc=1,
                  native_heap_used=None, native_heap_free=None, native_heap_arena=None,
                  main_stack_usable=None, main_stack_observed=None)
    return [case, [dict(sample, phase="baseline"), dict(sample, phase="released")], [],
            dict(stack_usable=None, stack_observed=None)]


def encode(data, complete=True):
    case, samples, workers, stack = data
    lines = ["RAM_CASE " + json.dumps(case)]
    lines += ["RAM_SAMPLE " + json.dumps(sample) for sample in samples]
    lines += ["RAM_WORKER " + json.dumps(worker) for worker in workers]
    lines += ["RAM_REPORT_STACK " + json.dumps(stack)]
    if complete:
        lines += ["RAM_END valid=true"]
        if case["domain"] == "nuttx-target":
            lines += ["RAM_COMMAND_EXIT status=0"]
    return "\n".join(lines)


def native_baseline():
    data = baseline()
    data[0].update(domain="nuttx-target", native_heap_available=True, native_stack_available=True)
    for sample in data[1]:
        sample.update(native_heap_used=1000, native_heap_free=9000, native_heap_arena=10000,
                      main_stack_usable=4096, main_stack_observed=1200)
    data[3].update(stack_usable=4096, stack_observed=1600)
    return data


class ReportTests(unittest.TestCase):
    def test_host_baseline_is_explicitly_not_native_ram(self):
        result = parse(encode(baseline()), "host-functional")
        self.assertFalse(result["accounting"]["linked_sections_included"])
        self.assertIsNone(result["deltas"][0]["native_heap_used_from_baseline"])

    def test_missing_completion_and_duplicate_phase_are_rejected(self):
        with self.assertRaises(ValueError):
            parse(encode(baseline(), complete=False), "host-functional")
        data = baseline()
        data[1].insert(1, copy.deepcopy(data[1][0]))
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")

    def test_host_bytes_cannot_be_relabelled_as_native_observations(self):
        data = baseline()
        data[1][0]["native_heap_used"] = 100
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")
        with self.assertRaises(ValueError):
            parse(encode(baseline()), "nuttx-target")

    def test_target_records_require_real_stack_fields(self):
        data = baseline()
        data[0].update(domain="nuttx-target", native_heap_available=True, native_stack_available=True)
        with self.assertRaises(ValueError):
            parse(encode(data), "nuttx-target")
        for sample in data[1]:
            sample.update(native_heap_used=1000, native_heap_free=9000, native_heap_arena=10000,
                          main_stack_usable=4096, main_stack_observed=1200)
        data[3].update(stack_usable=4096, stack_observed=1600)
        self.assertEqual(parse(encode(data), "nuttx-target")["reporting_stack"]["stack_observed"], 1600)

    def test_changed_baseline_and_counter_rollback_are_rejected(self):
        data = baseline()
        data[0].update(owners=1)
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")
        data = baseline()
        data[1][-1]["alloc"] = 1
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")

    def test_unreclaimed_baseline_is_rejected_even_when_delta_matches(self):
        data = baseline()
        data[1][-1]["rust_live"] += 8
        data[0]["released_rust_bytes_delta"] = 8
        with self.assertRaises(ValueError):
            parse(encode(data), "host-functional")

    def test_records_after_completion_are_rejected(self):
        text = encode(baseline()) + "\nRAM_SAMPLE " + json.dumps(baseline()[1][0])
        with self.assertRaises(ValueError):
            parse(text, "host-functional")

    def test_target_requires_successful_exit_and_rejects_late_failure(self):
        text = encode(native_baseline())
        for damaged in (text.replace("RAM_COMMAND_EXIT status=0", ""),
                        text.replace("RAM_COMMAND_EXIT status=0", "RAM_COMMAND_EXIT status=1"),
                        "RAM_COMMAND_EXIT status=0\n" + text,
                        text + "\nthread 'main' panicked at cleanup",
                        text + "\nassertion failed: exit",
                        text + "\nRAM_COMMAND_EXIT status=0"):
            with self.subTest(damaged=damaged), self.assertRaises(ValueError):
                parse(damaged, "nuttx-target")

    def test_native_heap_and_cumulative_main_stack_must_be_consistent(self):
        for sample_edit, report_edit in (({"native_heap_used": 1100}, {}),
                                        ({"main_stack_observed": 1100}, {}),
                                        ({"main_stack_usable": 8192}, {}),
                                        ({}, {"stack_observed": 1000}),
                                        ({}, {"stack_usable": 8192})):
            data = native_baseline()
            data[1][-1].update(sample_edit)
            data[3].update(report_edit)
            with self.subTest(sample=sample_edit, report=report_edit), self.assertRaises(ValueError):
                parse(encode(data), "nuttx-target")

    def test_duplicate_json_fields_are_rejected(self):
        text = encode(baseline()).replace('"owners": 0', '"owners": 20, "owners": 0')
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            parse(text, "host-functional")

    def test_booleans_cannot_replace_numeric_schema_or_lifecycle_fields(self):
        for key in ("schema", "workers_joined", "released_rust_bytes_delta"):
            data = baseline()
            data[0][key] = bool(data[0][key])
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse(encode(data), "host-functional")


SECTIONS = "  [ 1] .text PROGBITS 40374000 001000 000190 00 AX 0 0 4\n  [ 2] .bss NOBITS 3fc80000 002000 0003e8 00 WA 0 0 4\n"


def canonical_build(root):
    (root / "nuttx/include/nuttx").mkdir(parents=True)
    files = {"nuttx/nuttx": "ELF test fixture", "nuttx/nuttx.merged.bin": "firmware test fixture",
             "resolved.config": "CONFIG_BUILD_FLAT=y\nCONFIG_STACK_COLORATION=y\n",
             "provider-selection.json": "{}", "xtensa-esp32s3-nuttx.json": "{}"}
    for name, content in files.items():
        (root / name).write_text(content)
    hashes = {"/original/build/" + name: target_evidence.sha256(root / name) for name in files}
    hashes.update({"/original/repo/tests/ram-footprint-std/Cargo.toml": "a" * 64,
                   "/original/repo/tests/ram-footprint-std/ffi/observe.c": "b" * 64})
    (root / "binaries.sha256").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()))
    fields = dict(app_package="nxrs-ram-footprint-std", app_bin="ram-baseline", app_command="ramprobe",
                  platform="esp32s3-ram-footprint", abi_profile="minimal", target="xtensa-esp32s3-nuttx",
                  nuttx_build_mode="flat", nxrs_dirty="no", nxrs="c" * 40,
                  size_optimized="0", trace_only_backtrace="0", panic_immediate_abort="0")
    (root / "provenance.txt").write_text("".join(f"{key}={value}\n" for key, value in fields.items()))
    header = root / "nuttx/include/nuttx/config.h"
    header.write_text("#define CONFIG_BUILD_FLAT 1\n#define CONFIG_STACK_COLORATION 1\n")
    (root / "native-context.json").write_text(json.dumps({"schema": 1, "target": fields["target"],
        "config_sha256": target_evidence.sha256(header), "headers_sha256": "d" * 64}))


class TargetBindingTests(unittest.TestCase):
    def test_completed_target_capture_is_bound_to_binary_image_elf_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_build(root)
            with patch.object(target_evidence.subprocess, "check_output", return_value=SECTIONS):
                result = verify_target(encode(native_baseline()), root, "readelf", binary="ram-baseline",
                                       case="baseline", owners=0, stack_bytes=0)
            self.assertTrue(result["accounting"]["linked_sections_included"])
            self.assertEqual(result["build_identity"]["section_accounting"]["resident_ram_bytes"], 1400)
            self.assertEqual(result["build_identity"]["native_config_header_sha256"],
                             target_evidence.sha256(root / "nuttx/include/nuttx/config.h"))
            self.assertNotIn("/original", json.dumps(result))
            with self.assertRaisesRegex(ValueError, "invocation"):
                verify_target(encode(native_baseline()), root, "readelf", binary="ram-threads",
                              case="threads", owners=1, stack_bytes=4096)
            for name in ("nuttx/nuttx", "nuttx/nuttx.merged.bin", "resolved.config"):
                original = (root / name).read_bytes()
                (root / name).write_bytes(original + b"changed")
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "artifact differs"):
                    target_evidence.verify_build(root, "readelf", "ram-baseline")
                (root / name).write_bytes(original)

    def test_preflight_cli_requires_no_target_capture_or_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_build(root)
            output = root / "preflight.json"
            args = ["target_evidence.py", "--build", str(root), "--readelf", "readelf",
                    "--binary", "ram-baseline", "--out", str(output)]
            with patch("sys.argv", args), patch.object(target_evidence.subprocess, "check_output", return_value=SECTIONS):
                target_evidence.main()
            result = json.loads(output.read_text())
            self.assertFalse(result["device_io_performed"])
            self.assertFalse(result["physical_hardware_qualified"])
            self.assertEqual(result["build_identity"]["binary"], "ram-baseline")

    def test_observer_header_hash_and_controls_are_verified_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_build(root)
            header = root / "nuttx/include/nuttx/config.h"
            context = root / "native-context.json"
            original = json.loads(context.read_text())
            wrong = dict(original, config_sha256=target_evidence.sha256(root / "resolved.config"))
            context.write_text(json.dumps(wrong))
            with self.assertRaisesRegex(ValueError, "different target configuration"):
                target_evidence.verify_build(root, "readelf", "ram-baseline")
            header.write_text(header.read_text() + "#define CONFIG_SMP 1\n")
            original["config_sha256"] = target_evidence.sha256(header)
            context.write_text(json.dumps(original))
            with self.assertRaisesRegex(ValueError, "generated native configuration"):
                target_evidence.verify_build(root, "readelf", "ram-baseline")

    def test_wrong_build_or_observer_configuration_cannot_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_build(root)
            with self.assertRaisesRegex(ValueError, "identity/policy"):
                target_evidence.verify_build(root, "readelf", "ram-std-inbox")
            (root / "native-context.json").write_text(json.dumps({"config_sha256": "0" * 64}))
            with self.assertRaisesRegex(ValueError, "different target configuration"):
                target_evidence.verify_build(root, "readelf", "ram-baseline")


if __name__ == "__main__":
    unittest.main()
