"""Parser, durable-record and sanitized-report tests for control runs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load("test_control_measure", "control_measure.py")
matrix = load("test_control_matrix", "control_matrix.py")
report = load("test_control_report", "control_report.py")
legacy_measure = control.measure


class MinimalConsoleTests(unittest.TestCase):
    def test_saturation_uses_event_protocol_and_native_memory_snapshot(self):
        output = saturation_output(platform="nuttx-c", layout="three").encode()
        memory = b"1000 200 800 300 500 2 3 Umem\nevent> "
        with patch.object(control.os, "write") as write, \
             patch.object(control.measure, "_read_serial_until", return_value=output), \
             patch.object(control.measure, "_send_paced") as send, \
             patch.object(control.measure, "read_until_prompt", return_value=memory):
            row = control._run_saturation(73, "nuttx-c", "three", "event")
        self.assertEqual([call.args for call in write.call_args_list],
                         [(73, b"saturation\r"), (73, b"memory\r")])
        send.assert_not_called()
        self.assertEqual(row["free_memory"]["maxused"], 300)

    def test_minimal_console_cannot_silently_discard_an_invalid_memory_snapshot(self):
        with patch.object(control.measure.os, "write"), \
             patch.object(control.measure, "read_until_prompt", return_value=b"invalid\nevent> "):
            with self.assertRaises(control.measure.MeasurementOutputError):
                control._capture_free(73, "nuttx-c", "event")
        with self.assertRaisesRegex(ValueError, "snapshot is invalid"):
            control._validate_free("invalid", "nuttx-c", "event")
        self.assertIsNone(control._validate_free("invalid", "nuttx-c", "nsh"))

    def test_matrix_command_preserves_console_identity(self):
        command = matrix.command("nuttx-rust-three", "image.bin", "out", "PORT",
                                 "FLASHER", 2, ["normal"], "event")
        self.assertEqual(command[command.index("--nuttx-console") + 1], "event")


def traffic_output(
    profile="normal", platform="embassy", layout="one", *, control_edits=None
):
    attempts = legacy_measure.EXPECTED_ATTEMPTS[profile]
    queues = 20 if layout == "one" else 60
    capacity = 24 if layout == "one" else 8
    service_rows = [
        f"ES_SERVICE id={service} received={attempts // 20} "
        "start_p99_us=6 start_max_us=10 finish_p99_us=12 control_p99_us=4 "
        "queue_p99_us=3 queue_max_us=7 missed_control=0 missed_data=0 "
        "missed_status=0 rejected=0"
        for service in range(20)
    ]
    result = {
        "platform": platform,
        "profile": profile,
        "services": 20,
        "queues": queues,
        "event_bytes": 64,
        "slots": 480,
        "attempted": attempts,
        "accepted": attempts,
        "received": attempts,
        "rejected": 0,
        "errors": 0,
        "missed": 0,
        "publication_p99_us": 3,
        "publication_max_us": 4,
        "start_p99_us": 6,
        "start_max_us": 10,
        "finish_p99_us": 12,
        "finish_max_us": 20,
        "control_p99_us": 4,
        "control_max_us": 6,
        "worst_service_p99_us": 6,
        "queue_peak_observed": capacity,
        "last_finish_us": 2_500_000,
        "delivery_ok": 1,
        "capacity_ok": 1,
        "deadlines_ok": 1,
        "queue_p99_us": 4,
        "queue_max_us": 9,
    }
    iterations = {"work-short": 10_000, "work-medium": 100_000, "work-long": 400_000}.get(profile, 0)
    control_row = {
        "timer_ms": 1,
        "work_iterations": iterations,
        "work_jobs": 117 if iterations else 0,
        "work_p99_us": 6 if iterations else 0,
        "work_max_us": 9 if iterations else 0,
        "hal_calls": 21 if profile == "hal" else 0,
        "hal_errors": 0,
        "diagnostic_bytes": 276,
    }
    control_row.update(control_edits or {})
    lines = [
        *service_rows,
        "ES_RESOURCES queue_buffers=30720 queue_objects=800 "
        "thread_objects=400 entry_storage=300 stack_storage=81920 "
        "heap_allocated=0 diagnostics_queue_peaks=80 note=stable",
        "ES_MEMORY application_state=3000 diagnostics=10000 heap_before=100 heap_live=200",
        "ES_CONTROL "
        + " ".join(f"{key}={value}" for key, value in control_row.items()),
        "ES_RESULT " + " ".join(f"{key}={value}" for key, value in result.items()),
        "ES_PASS",
        "ES_COMMAND_EXIT status=0",
    ]
    return "\n".join(lines) + "\n"


def saturation_output(
    platform="embassy", layout="one", *, damage=None, heap_samples=(512, 512, 512)
):
    queues = 20 if layout == "one" else 60
    stack_bytes = 8192 if platform == "embassy" else 81920
    resources = [
        "ES_RESOURCES queue_buffers=30720 queue_objects=800 "
        f"thread_objects=400 entry_storage=300 stack_storage={stack_bytes} "
        f"heap_allocated={used} note=held"
        for used in heap_samples
    ]
    cycles = []
    for cycle in range(3):
        values = {
            "cycle": cycle,
            "empty_heap": 100,
            "full_heap": 30_820,
            "drained_heap": 100,
            "filled": 480,
            "drained": 480,
            "overflow_rejected": queues,
            "depth_full": queues,
            "depth_zero": queues,
            "errors": 0,
        }
        if damage == "cycle-error" and cycle == 1:
            values["errors"] = 1
        cycles.append(
            "ES_SAT_CYCLE "
            + " ".join(f"{key}={value}" for key, value in values.items())
        )
    result = (
        "ES_SAT_RESULT "
        f"platform={platform} queues={queues} slots=480 event_bytes=64 "
        f"cycles=3 errors={int(damage == 'result-error')} stacks={stack_bytes}"
    )
    if damage == "resources-missing":
        resources = resources[:2]
    return (
        "\n".join([*resources, *cycles, result, "ES_PASS", "ES_COMMAND_EXIT status=0"])
        + "\n"
    )


def minimal_provenance(directory, case="embassy-one"):
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "image.bin"
    elf = directory / "app.elf"
    image.write_bytes(b"frozen image")
    elf.write_bytes(b"frozen elf")
    platform, layout = case.rsplit("-", 1)
    provenance = {
        "status": "success",
        "failure": None,
        "platform": platform,
        "layout": layout,
        "image_file": image.name,
        "elf_file": elf.name,
        "artifacts": {
            image.name: hashlib.sha256(image.read_bytes()).hexdigest(),
            elf.name: hashlib.sha256(elf.read_bytes()).hexdigest(),
        },
        "artifact_bytes": {
            image.name: image.stat().st_size,
            elf.name: elf.stat().st_size,
        },
        "source_sha256": {"platform.c": "a" * 64},
        "kernel_config_identity": "b" * 64,
        "configuration": {
            "cpu_mhz": 240,
            "cores": 1,
            "publication_timer_resolution_ms": 1,
        },
        "section_accounting": {
            "loadbearing_flash_bytes": 1000,
            "resident_ram_bytes": 2000,
            "flash_sections": [".text"],
            "resident_ram_sections": [".bss"],
            "excluded_dummy_padding": [],
        },
    }
    (directory / "build-provenance.json").write_text(json.dumps(provenance))
    return image


class ControlParserTests(unittest.TestCase):
    def test_full_queues_preserve_reduced_work_as_capacity_failure_evidence(self):
        text = traffic_output('work-long', control_edits={'work_jobs': 100})
        parsed = control.validate_control_row(text, 'work-long', rejected=17, service_received=178)
        self.assertEqual(parsed['work_jobs'], 100)
        for rejects, received in ((0, 178), (16, 178), (17, 179)):
            with self.subTest(rejects=rejects, received=received), self.assertRaises(ValueError):
                control.validate_control_row(text, 'work-long', rejected=rejects, service_received=received)

    def test_all_traffic_controls_validate_workload_contracts(self):
        for profile in control.CONTROL_TRAFFIC_PROFILES:
            with self.subTest(profile=profile):
                parsed = control.validate_traffic_output(
                    traffic_output(profile), profile, "one", "embassy"
                )
                expected = legacy_measure.EXPECTED_ATTEMPTS[profile]
                self.assertEqual(parsed["result"]["attempted"], expected)
                self.assertEqual(
                    parsed["control"]["hal_calls"], 21 if profile == "hal" else 0
                )
        self.assertEqual(legacy_measure.EXPECTED_ATTEMPTS["overload"], 192600)

    def test_control_marker_rejects_bad_jobs_quantiles_and_duplicate_rows(self):
        for edits in ({"work_jobs": 116}, {"work_p99_us": 10, "work_max_us": 9}):
            text = traffic_output("work-short", control_edits=edits)
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                control.validate_traffic_output(text, "work-short", "one", "embassy")
        duplicated = traffic_output("normal") + "ES_CONTROL timer_ms=1\n"
        with self.assertRaises(ValueError):
            control.validate_traffic_output(duplicated, "normal", "one", "embassy")

    def test_saturation_requires_three_healthy_cycles_and_resource_snapshots(self):
        parsed = control.validate_saturation_output(
            saturation_output("zephyr-c", "three"), "zephyr-c", "three"
        )
        self.assertEqual(len(parsed["cycles"]), 3)
        self.assertEqual(len(parsed["resources"]), 3)
        warmed = control.validate_saturation_output(
            saturation_output(heap_samples=(400, 512, 512)), "embassy", "one"
        )
        self.assertEqual(
            [row["heap_allocated"] for row in warmed["resources"]], [400, 512, 512]
        )
        for damage in ("cycle-error", "result-error", "resources-missing"):
            with self.subTest(damage=damage), self.assertRaises(ValueError):
                control.validate_saturation_output(
                    saturation_output(damage=damage), "embassy", "one"
                )

    def test_record_reparses_raw_hash_and_rejects_tampering(self):
        raw = traffic_output("work-long")
        parsed = control.validate_traffic_output(raw, "work-long", "one", "embassy")
        entry = {
            "profile": "work-long",
            **parsed,
            "raw_output": raw,
            "raw_output_sha256": hashlib.sha256(raw.encode("latin-1")).hexdigest(),
            "free_raw_output": "",
            "free_memory": None,
        }
        record = {
            "contract_version": 1,
            "failure": None,
            "completed_runs": 1,
            "requested_runs": 1,
            "image_sha256": "c" * 64,
            "runs": [entry],
        }
        checked = control.verify_record(
            record,
            image_sha256="c" * 64,
            platform="embassy",
            layout="one",
            profiles=["work-long"],
            runs=1,
        )
        self.assertEqual(checked[0]["control"]["work_iterations"], 400000)
        self.assertEqual(
            control.validate_build_timer(
                checked, {"publication_timer_resolution_ms": 1}
            ),
            1,
        )
        with self.assertRaisesRegex(ValueError, "disagrees"):
            control.validate_build_timer(
                checked, {"publication_timer_resolution_ms": 10}
            )
        record["runs"][0]["raw_output"] += "tampered"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            control.verify_record(
                record,
                image_sha256="c" * 64,
                platform="embassy",
                layout="one",
                profiles=["work-long"],
                runs=1,
            )


class ControlMatrixReportTests(unittest.TestCase):
    def test_traffic_report_retains_every_service_and_resource_breakdown(self):
        parsed = control.validate_traffic_output(
            traffic_output("work-medium"), "work-medium", "one", "embassy"
        )
        row = {
            **parsed,
            "block": 0,
            "run_index": 0,
            "qualified": True,
            "profile": "work-medium",
            "free_memory": None,
            "raw_output_sha256": "d" * 64,
            "work_peer0": {"start_p99_us": 6},
            "worst_nonzero_service_start_p99_us": 6,
            "worst_nonzero_service_queue_p99_us": 3,
        }
        source = {
            "platform": "embassy",
            "layout": "one",
            "image_sha256": "a" * 64,
            "elf_sha256": "b" * 64,
            "configuration": {"publication_timer_resolution_ms": 1},
            "kernel_config_identity": None,
            "source_sha256": {"core.rs": "c" * 64},
            "section_accounting": {
                "loadbearing_flash_bytes": 1000,
                "resident_ram_bytes": 2000,
            },
            "artifact_bytes": {"image.bin": 3000, "app.elf": 4000},
            "blocks": [0],
            "runs": [row],
        }
        validated = (
            {"backup_sha256": "e" * 64}, ["work-medium"], 1, 1,
            {"embassy-one": source},
        )
        with patch.object(report, "_validate_matrix", return_value=validated):
            output = report.build_report(Path("unused-control-matrix.json"))
        exported = output["cases"]["embassy-one"]["profiles"]["work-medium"]["runs"][0]
        self.assertEqual(len(exported["services"]), 20)
        self.assertEqual(exported["services"], parsed["services"])
        self.assertEqual(exported["resources"], parsed["resources"])
        self.assertEqual(exported["memory"], parsed["memory"])
        self.assertNotIn("raw_output", exported)

    def test_matrix_validates_private_artifacts_restores_and_exports_sanitized_report(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            image = minimal_provenance(artifacts / "embassy-one")
            backup = root / "backup.bin"
            with backup.open("wb") as stream:
                stream.truncate(16_777_216)
            backup.chmod(0o600)
            out = root / "matrix-out"
            argv = [
                "control_matrix.py",
                "--artifacts",
                str(artifacts),
                "--out",
                str(out),
                "--backup",
                str(backup),
                "--port",
                "PORT",
                "--flasher",
                "FLASHER",
                "--case",
                "embassy-one",
                "--runs",
                "1",
                "--blocks",
                "1",
                "--profile",
                "saturation",
            ]

            def fake_measure(command, check):
                target = Path(command[command.index("--out") + 1])
                target.mkdir()
                raw = saturation_output()
                parsed = control.validate_saturation_output(raw, "embassy", "one")
                record = {
                    "schema": 1,
                    "contract_version": 1,
                    "failure": None,
                    "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                    "requested_runs": 1,
                    "completed_runs": 1,
                    "runs": [
                        {
                            "profile": "saturation",
                            **parsed,
                            "raw_output": raw,
                            "raw_output_sha256": hashlib.sha256(
                                raw.encode()
                            ).hexdigest(),
                            "free_raw_output": "",
                            "free_memory": None,
                        }
                    ],
                }
                (target / "measurement.json").write_text(json.dumps(record))
                return 0

            with patch.object(matrix.sys, "argv", argv), patch.object(
                matrix.subprocess, "run", side_effect=fake_measure
            ), patch.object(matrix.matrix, "restore") as restore:
                matrix.main()
            restore.assert_called_once()
            matrix_record = json.loads((out / "control-matrix.json").read_text())
            self.assertTrue(matrix_record["restore_verified"])
            self.assertEqual(len(matrix_record["order"][0]["runs"]), 1)

            output = report.build_report(out / "control-matrix.json")
            case = output["cases"]["embassy-one"]
            self.assertEqual(case["flash_and_ram"]["whole_ram_peak_bytes"], 2000)
            self.assertEqual(case["flash_and_ram"]["nfree_unavailable_run_count"], 0)
            sat = case["profiles"]["saturation"]
            self.assertTrue(sat["heap_stability"]["drained_heap"])
            self.assertEqual(
                sat["heap_metrics"]["full_minus_empty_heap"],
                {"median": 30720, "max": 30720},
            )
            self.assertEqual(
                sat["resource_heap_allocated_metrics"],
                {"median": 512, "max": 512},
            )
            exported = json.dumps(output)
            self.assertNotIn("serial.log", exported)
            self.assertNotIn('raw_output"', exported)
            self.assertNotIn(str(root), exported)


if __name__ == "__main__":
    unittest.main()
