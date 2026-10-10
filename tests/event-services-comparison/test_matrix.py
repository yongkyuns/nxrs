"""Artifact validation and restoration tests with no connected device."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("event_matrix_tests", Path(__file__).with_name("run_matrix.py"))
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def write_frozen(directory, case="embassy-one", *, artifact_name="firmware.bin"):
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / artifact_name
    elf = directory / "firmware.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"frozen-image")
    elf.write_bytes(b"frozen-elf")
    provenance = {
        "status": "success", "failure": None,
        "artifacts": {artifact_name: hashlib.sha256(image.read_bytes()).hexdigest(),
                      "firmware.elf": hashlib.sha256(elf.read_bytes()).hexdigest()},
        "image_file": artifact_name, "elf_file": "firmware.elf",
        "layout": matrix.CASES[case][1], "platform": matrix.CASES[case][0],
        "source_sha256": {"source.c": "a" * 64},
        "artifact_bytes": {artifact_name: image.stat().st_size, "firmware.elf": elf.stat().st_size},
        "configuration": {"cpu_mhz": 240}, "kernel_config_identity": "b" * 64,
        "section_accounting": {"resident_ram_bytes": 123},
    }
    (directory / "build-provenance.json").write_text(json.dumps(provenance))
    return image


class FrozenArtifactTests(unittest.TestCase):
    def test_valid_frozen_directory_and_all_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "embassy-one"
            image = write_frozen(directory)
            frozen = matrix.frozen_image(directory, "embassy-one")
            self.assertEqual(frozen["image"], image)
            self.assertEqual(frozen["elf"], directory / "firmware.elf")
            self.assertEqual(frozen["artifact_bytes"]["firmware.bin"], len(b"frozen-image"))
            image.write_bytes(b"altered")
            with self.assertRaisesRegex(ValueError, "changed frozen"):
                matrix.frozen_image(directory, "embassy-one")

    def test_matrix_numeric_run_keeps_validated_free_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image.bin"
            image.write_bytes(b"image")
            output = root / "run"
            output.mkdir()
            raw_hash = "c" * 64
            (output / "measurement.json").write_text(json.dumps({
                "runs": [{"raw_output_sha256": raw_hash}],
            }))
            validated = [{"profile": "normal", "result": {"attempted": 3900},
                          "services": [], "resources": {"queue_buffers": 30720},
                          "memory": {"heap_live": 1},
                          "free_memory": {"total": 100, "used": 40, "free": 60,
                                          "maxused": 50, "maxfree": 70,
                                          "nused": 2, "nfree": 3}}]
            with patch.object(matrix.measure, "verify_measurement_record", return_value=validated):
                rows = matrix._measurement_rows(output, image, "nuttx-c-one", ["normal"], 1)
            self.assertEqual(rows[0]["free_memory"], validated[0]["free_memory"])

    def test_rejects_traversal_absolute_and_symlink_escape(self):
        for unsafe in ("../outside.bin", "/tmp/outside.bin", "nested/../outside.bin"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "embassy-one"
                write_frozen(directory)
                provenance_path = directory / "build-provenance.json"
                provenance = json.loads(provenance_path.read_text())
                provenance["artifacts"][unsafe] = "0" * 64
                provenance["image_file"] = unsafe
                provenance_path.write_text(json.dumps(provenance))
                with self.assertRaises(ValueError):
                    matrix.frozen_image(directory, "embassy-one")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "embassy-one"
            write_frozen(directory)
            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            (directory / "escape.bin").symlink_to(outside)
            provenance_path = directory / "build-provenance.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["artifacts"]["escape.bin"] = hashlib.sha256(outside.read_bytes()).hexdigest()
            provenance_path.write_text(json.dumps(provenance))
            with self.assertRaisesRegex(ValueError, "escapes"):
                matrix.frozen_image(directory, "embassy-one")

    def test_rejects_failed_build_and_hash_map_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "nuttx-c-three"
            write_frozen(directory, case="nuttx-c-three")
            path = directory / "build-provenance.json"
            provenance = json.loads(path.read_text())
            provenance["failure"] = "compile failed"
            path.write_text(json.dumps(provenance))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                matrix.frozen_image(directory, "nuttx-c-three")

    def test_case_order_and_command_profile_arguments(self):
        cases = ["nuttx-c-one", "embassy-one", "zephyr-c-one"]
        self.assertEqual(matrix.rotated_cases(cases, 1), cases[1:] + cases[:1])
        command = matrix.command("embassy-one", Path("/frozen/image.bin"), Path("/out"),
                                 "PORT", "FLASHER", 3, ["burst", "normal"])
        self.assertIn("--platform", command)
        self.assertEqual(command.count("--profile"), 2)
        self.assertEqual(command[command.index("--runs") + 1], "3")


class RestoreTests(unittest.TestCase):
    def test_matrix_entry_carries_artifact_configuration_and_source_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            image = write_frozen(artifacts / "embassy-one")
            backup, output = root / "private.bin", root / "matrix-out"
            with backup.open("wb") as stream:
                stream.truncate(16_777_216)
            backup.chmod(0o600)
            argv = ["run_matrix.py", "--artifacts", str(artifacts), "--out", str(output),
                    "--backup", str(backup), "--port", "PORT", "--flasher", "FLASHER",
                    "--case", "embassy-one", "--runs", "1", "--blocks", "1",
                    "--profile", "normal"]

            def mocked_measure(command, check):
                out_dir = Path(command[command.index("--out") + 1])
                out_dir.mkdir()
                return subprocess.CompletedProcess(command, 0)

            sample = {"profile": "normal", "result": {"attempted": 3900},
                      "services": [], "resources": {}, "memory": {},
                      "free_memory": None, "raw_output_sha256": "c" * 64}
            with patch.object(matrix.sys, "argv", argv), \
                 patch.object(matrix.subprocess, "run", side_effect=mocked_measure), \
                 patch.object(matrix, "_measurement_rows", return_value=[sample]), \
                 patch.object(matrix, "restore"):
                matrix.main()
            entry = json.loads((output / "matrix.json").read_text())["order"][0]
            self.assertEqual(entry["artifact_bytes"], {
                "firmware.bin": image.stat().st_size, "firmware.elf": (image.parent / "firmware.elf").stat().st_size})
            self.assertEqual(entry["configuration"], {"cpu_mhz": 240})
            self.assertEqual(entry["kernel_config_identity"], "b" * 64)
            self.assertEqual(entry["source_sha256"], {"source.c": "a" * 64})
            self.assertEqual(entry["section_accounting"], {"resident_ram_bytes": 123})
            self.assertEqual(entry["runs"][0]["free_memory"], None)

    def test_failure_after_measurement_still_restores_and_records_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup, output = root / "private.bin", root / "matrix-out"
            with backup.open("wb") as stream:
                stream.truncate(16_777_216)
            backup.chmod(0o600)
            argv = ["run_matrix.py", "--artifacts", str(root), "--out", str(output),
                    "--backup", str(backup), "--port", "PORT", "--flasher", "FLASHER",
                    "--case", "embassy-one", "--runs", "1", "--blocks", "1",
                    "--profile", "normal"]
            image = root / "fake.bin"
            image.write_bytes(b"frozen")
            with patch.object(matrix.sys, "argv", argv), \
                 patch.object(matrix, "frozen_image", return_value={"image": image, "elf": image, "provenance": {}}), \
                 patch.object(matrix.subprocess, "run",
                              side_effect=subprocess.CalledProcessError(1, ["measure"])) as run, \
                 patch.object(matrix, "restore") as restore:
                with self.assertRaises(subprocess.CalledProcessError):
                    matrix.main()
            self.assertEqual(run.call_count, 1)
            restore.assert_called_once_with("FLASHER", "PORT", backup, output)
            record = json.loads((output / "matrix.json").read_text())
            self.assertTrue(record["restore_verified"])
            self.assertIn("CalledProcessError", record["failure"])
            self.assertEqual(record["order"], [])

    def test_backup_must_be_private_and_exactly_sixteen_mib(self):
        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary) / "backup.bin"
            backup.write_bytes(b"short")
            with self.assertRaisesRegex(ValueError, "16 MiB"):
                matrix.backup_identity(backup)
            with backup.open("wb") as stream:
                stream.truncate(16_777_216)
            backup.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                matrix.backup_identity(backup)


if __name__ == "__main__":
    unittest.main()
