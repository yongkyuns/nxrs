"""Build metadata selects layouts; addresses and nearby unrelated files do not."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "layout_discovery.py"
SPEC = importlib.util.spec_from_file_location("layout_discovery", SCRIPT)
DISCOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISCOVERY)
REVISION = "2b5c0de5e933281c209f954a63b99cf10c132346"


class LayoutDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "nuttx").mkdir()
        self.elf = self.root / "nuttx/nuttx"
        self.elf.write_bytes(b"test identity, not a hardware executable")
        self.identity = hashlib.sha256(self.elf.read_bytes()).hexdigest()

    def receipt(self, board="mps2-an521:nsh", config=None):
        config = config or "CONFIG_ARCH_BOARD_MPS2_AN521=y\nCONFIG_ARCH_CHIP_MPS2_AN521=y\n"
        (self.root / "resolved.config").write_bytes(config.encode())
        (self.root / "provenance.txt").write_text(f"board={board}\nnuttx={REVISION}\n")
        # Absolute source paths deliberately belong to another machine. The
        # retained relative artifact suffix and digest survive relocation.
        (self.root / "binaries.sha256").write_text(
            f"{self.identity}  /original/build/nuttx/nuttx\n"
            f"{hashlib.sha256(config.encode()).hexdigest()}  /original/build/resolved.config\n")

    def discover(self, **kwargs):
        return DISCOVERY.discover(self.elf, self.identity, **kwargs)

    def test_relocated_completed_build_selects_pinned_board_and_binds_layout(self):
        self.receipt()
        (self.elf.parent / "nuttx.map").write_text("map fixture")
        result = self.discover()
        self.assertEqual(result["map"], self.elf.parent / "nuttx.map")
        self.assertEqual(result["layout"].name, "mps2-an521-qemu.json")
        self.assertEqual(result["layout_data"]["build"]["elf_sha256"], self.identity)
        self.assertTrue(result["automatic_layout"])
        self.assertTrue(any("pinned BSP" in warning for warning in result["warnings"]))

    def test_bare_addresses_and_unbound_sidecar_do_not_select_physical_board(self):
        self.elf.write_bytes(b"0x10000000 0x3fc88000 0x20000000")
        layout = self.elf.parent / "layout.json"
        layout.write_text(json.dumps({"schema_version": 1, "regions": []}))
        result = self.discover()
        self.assertIsNone(result["layout"])
        self.assertTrue(any("no build.elf_sha256" in warning for warning in result["warnings"]))
        explicit = self.discover(layout=layout)
        self.assertEqual(explicit["layout"], layout)
        self.assertFalse(explicit["automatic_layout"])

    def test_changed_elf_or_config_or_revision_does_not_apply_a_stale_board(self):
        self.receipt()
        (self.root / "resolved.config").write_text("CONFIG_ARCH_BOARD_MPS2_AN521=n\n")
        result = self.discover()
        self.assertIsNone(result["layout"])
        self.assertTrue(any("does not match" in warning for warning in result["warnings"]))
        self.receipt()
        result = DISCOVERY.discover(self.elf, "a" * 64)
        self.assertIsNone(result["layout"])
        self.assertTrue(any("different ELF" in warning for warning in result["warnings"]))
        self.receipt()
        (self.root / "provenance.txt").write_text("board=mps2-an521:nsh\nnuttx=" + "f" * 40 + "\n")
        self.assertIsNone(self.discover()["layout"])

    def test_bound_sidecar_wins_over_receipt_and_wrong_identity_is_rejected(self):
        self.receipt()
        layout = self.elf.parent / "layout.json"
        layout.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": self.identity}, "regions": []}))
        self.assertEqual(self.discover()["layout"], layout)
        layout.write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": "b" * 64}, "regions": []}))
        with self.assertRaisesRegex(ValueError, "different ELF"):
            self.discover(layout=layout)
        self.assertEqual(self.discover()["layout"].name, "mps2-an521-qemu.json")

    def test_ambiguous_matching_sidecars_and_maps_need_explicit_choice(self):
        elf = self.root / "example.elf"
        elf.write_bytes(self.elf.read_bytes())
        for name in ("example.layout.json", "layout.json"):
            (self.root / name).write_text(json.dumps({"schema_version": 1, "build": {"elf_sha256": self.identity}, "regions": []}))
        (self.root / "example.map").write_text("first map")
        (self.root / "example.elf.map").write_text("second map")
        result = DISCOVERY.discover(elf, self.identity)
        self.assertIsNone(result["map"])
        self.assertIsNone(result["layout"])
        self.assertEqual(sum("Multiple" in warning for warning in result["warnings"]), 2)
        explicit = DISCOVERY.discover(elf, self.identity, map_path=self.root / "example.map", layout=self.root / "layout.json")
        self.assertEqual(explicit["map"].name, "example.map")
        self.assertEqual(explicit["layout"].name, "layout.json")

    def test_core_s3_configured_memory_has_no_invented_address_mapping(self):
        self.receipt(board="esp32s3-m5-cores3:nsh", config=(
            "CONFIG_ARCH_BOARD_ESP32S3_M5_CORES3=y\nCONFIG_ARCH_CHIP_ESP32S3=y\n"
            "CONFIG_ESP32S3_SPIRAM=y\nCONFIG_ESP32S3_SPIRAM_SIZE=8388608\n"
            "CONFIG_ESP32S3_FLASH_16M=y\n"))
        result = self.discover()
        self.assertIsNotNone(result["layout_data"])
        resources = result["layout_data"]["physical_resources"]
        psram = next(item for item in resources if item["id"] == "configured-psram")
        self.assertEqual(psram["size"], 8 * 1024 * 1024)
        self.assertEqual(next(item["size"] for item in resources if item["id"] == "configured-flash"), 16 * 1024 * 1024)
        self.assertFalse(any(region.get("physical", {}).get("id") in ("configured-psram", "configured-flash")
                             for region in result["layout_data"]["regions"]))
        self.assertTrue(any("remains unmapped" in warning for warning in result["layout_data"]["warnings"]))

    def test_unknown_or_ambiguous_configured_capacity_stays_unknown(self):
        entries = DISCOVERY.catalogue()
        selected = next(entry for entry in entries if entry["id"] == "esp32s3-cores3")
        context = {"elf_sha256": self.identity, "config": {
            "CONFIG_ESP32S3_SPIRAM": "y", "CONFIG_ESP32S3_SPIRAM_SIZE": "-1",
            "CONFIG_ESP32S3_FLASH_4M": "y", "CONFIG_ESP32S3_FLASH_16M": "y"}}
        layout = DISCOVERY._specialize(selected, context)
        self.assertFalse(any(resource["id"] in ("configured-psram", "configured-flash") for resource in layout["physical_resources"]))
        self.assertEqual(sum("capacity remains unknown" in warning for warning in layout["warnings"]), 2)


if __name__ == "__main__":
    unittest.main()
