"""Check bundled geometry against pinned BSP inputs, not runtime inference."""

import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock


TOOL = Path(__file__).resolve().parents[1]
REPOSITORY = TOOL.parents[1]
LAYOUTS = TOOL / "layouts"
REFERENCES = REPOSITORY / "upstream" / "memory-layouts"
SPEC = importlib.util.spec_from_file_location("refresh_bsp", TOOL / "refresh_bsp.py")
BSP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BSP)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def macros(path):
    text = path.read_text(encoding="utf-8")
    return {name: int(value, 16) for name, value in re.findall(
        r"^#define\s+(\w+)\s+(0x[0-9a-fA-F]+)\b", text, re.MULTILINE)}


class CatalogGeometryTests(unittest.TestCase):
    def test_all_declared_native_bsps_are_covered_or_explicitly_process_scoped(self):
        catalog = read_json(LAYOUTS / "catalog.json")
        native = {board["nuttx_board"] for board in catalog["boards"]}
        processes = set(catalog["process_targets"])
        selected = set()
        for base in (REPOSITORY / "platform", REPOSITORY / "tests" / "profiles"):
            for path in base.rglob("*.toml"):
                profile = tomllib.loads(path.read_text(encoding="utf-8"))
                if isinstance(profile.get("board"), str):
                    selected.add(profile["board"])
        self.assertTrue(selected)
        self.assertEqual(selected - processes, native)
        self.assertEqual(selected & processes, {"sim:nsh"})
        self.assertIn("host process", catalog["process_targets"]["sim:nsh"].lower())
        self.assertEqual(catalog["source_manifest"], "upstream/memory-layouts/sources.json")

    def test_every_retained_source_and_rust_selector_matches_the_reviewed_catalog(self):
        result = BSP.check()
        self.assertEqual(result["boards"], 5)
        self.assertGreater(result["sources"], 10)
        self.assertLess(result["bytes"], 512 * 1024)
        manifest = read_json(REFERENCES / "sources.json")
        expected = subprocess.check_output(
            ["git", "rev-parse", ":external/nuttx"],
            cwd=REPOSITORY, text=True).strip()
        self.assertEqual(manifest["pins"]["nuttx"]["revision"], expected)
        for local in manifest["license_files"].values():
            self.assertIn(local, {item["local"] for item in manifest["sources"]})

    def test_esp_sram_windows_union_the_pinned_vendor_aliases_once(self):
        values = macros(REFERENCES / "sources/esp-hal-3rdparty/components/soc/esp32s3/include/soc/soc.h")
        layout = read_json(LAYOUTS / "esp32s3.json")
        regions = {item["id"]: item for item in layout["regions"]}
        resource = next(item for item in layout["physical_resources"] if item["id"] == "esp32s3-internal-sram")
        iram, dram = regions["esp32s3-iram"], regions["esp32s3-dram"]
        for region, prefix in ((iram, "SOC_IRAM"), (dram, "SOC_DRAM")):
            self.assertEqual(region["start"], values[prefix + "_LOW"])
            self.assertEqual(region["size"], values[prefix + "_HIGH"] - values[prefix + "_LOW"])
        instruction_only = values["SOC_DIRAM_IRAM_LOW"] - values["SOC_IRAM_LOW"]
        shared = values["SOC_DIRAM_IRAM_HIGH"] - values["SOC_DIRAM_IRAM_LOW"]
        data_only = values["SOC_DRAM_HIGH"] - values["SOC_DIRAM_DRAM_HIGH"]
        self.assertEqual(resource["size"], instruction_only + shared + data_only)
        self.assertEqual(resource["size"], 512 * 1024)
        self.assertEqual([item["size"] for item in resource["banks"]], [instruction_only, shared, data_only])
        self.assertEqual(iram["physical"]["offset"], 0)
        self.assertEqual(dram["physical"]["offset"], instruction_only)
        # The first shared byte is the same resource byte through both buses.
        self.assertEqual(
            iram["physical"]["offset"] + values["SOC_DIRAM_IRAM_LOW"] - iram["start"],
            dram["physical"]["offset"] + values["SOC_DIRAM_DRAM_LOW"] - dram["start"])
        self.assertEqual(iram["size"] + dram["size"] - shared, resource["size"])
        self.assertEqual(regions["esp32s3-rtc-fast"]["start"], values["SOC_RTC_IRAM_LOW"])
        self.assertEqual(values["SOC_RTC_IRAM_LOW"], values["SOC_RTC_DRAM_LOW"])
        self.assertEqual(sum(item["size"] for item in layout["physical_resources"]), 528 * 1024)

    def test_esp_external_windows_never_invent_linear_flash_or_psram_backing(self):
        values = macros(REFERENCES / "sources/esp-hal-3rdparty/components/soc/esp32s3/include/soc/soc.h")
        layout = read_json(LAYOUTS / "esp32s3.json")
        by_id = {item["id"]: item for item in layout["regions"]}
        for id, prefix in (("esp32s3-external-data", "SOC_DROM"), ("esp32s3-external-instructions", "SOC_IROM")):
            region = by_id[id]
            self.assertEqual(region["start"], values[prefix + "_LOW"])
            self.assertEqual(region["size"], values[prefix + "_HIGH"] - values[prefix + "_LOW"])
            self.assertNotIn("physical", region)
            self.assertEqual(region["kind"], "unknown")
        source = (REFERENCES / "sources/nuttx/arch/xtensa/src/esp32s3/Kconfig").read_text()
        size_block = source.split("config ESP32S3_SPIRAM_SIZE\n", 1)[1].split("\nconfig ", 1)[0]
        declared = {int(size) for size in re.findall(r"default (\d+) if ESP32S3_SPIRAM_TYPE_", size_block)}
        rules = {rule["id"]: rule for rule in layout["configured_resources"]}
        self.assertEqual(set(rules["configured-psram"]["allowed_sizes"]), declared)
        self.assertNotIn(-1, declared)  # AUTO detection is not a retained capacity.
        self.assertEqual(rules["configured-psram"]["enabled_by"], "CONFIG_ESP32S3_SPIRAM")
        for key, size in rules["configured-flash"]["size_choices"].items():
            self.assertIn("config " + key.removeprefix("CONFIG_") + "\n", source)
            self.assertEqual(int(re.search(r"_(\d+)M$", key).group(1)) * 1024 * 1024, size)

    def test_esp_hal_pin_is_the_dependency_selected_by_the_pinned_nuttx(self):
        manifest = read_json(REFERENCES / "sources.json")
        make = (REFERENCES / "sources/nuttx/arch/xtensa/src/esp32s3/Make.defs").read_text()
        selected = re.search(r"ESP_HAL_3RDPARTY_VERSION\s*=\s*([0-9a-f]{40})", make).group(1)
        self.assertEqual(manifest["pins"]["esp-hal-3rdparty"]["revision"], selected)
        patch = "upstream/nuttx/patches/0104-esp32s3-m5-cores3-raw-resources.patch"
        retained = (REFERENCES / "sources/nxrs" / patch).read_text()
        native = "boards/xtensa/esp32s3/esp32s3-m5-cores3/scripts/Make.defs"
        addition = retained.split("+++ b/" + native + "\n", 1)[1].split("\ndiff --git ", 1)[0]
        original = "".join(line[1:] + "\n" for line in addition.splitlines() if line.startswith("+"))
        board = REPOSITORY / "platform/cores3/board"
        self.assertEqual((board / "scripts/Make.defs").read_text(), original)
        config = (board / "configs/nsh/defconfig").read_text().splitlines()
        for setting in ("CONFIG_ARCH_BOARD_CUSTOM=y",
                        'CONFIG_ARCH_BOARD_CUSTOM_NAME="esp32s3-m5-cores3"',
                        "CONFIG_ARCH_BOARD_COMMON=y"):
            self.assertIn(setting, config)

    def test_pico_extents_match_all_three_bsp_linker_modes_and_both_defconfigs(self):
        base = REFERENCES / "sources/nuttx/boards/arm/rp23xx/raspberrypi-pico-2"
        layout = read_json(LAYOUTS / "raspberrypi-pico-2.json")
        resource = layout["physical_resources"][0]
        sram = next(item for item in layout["regions"] if item["id"] == "rp2350-sram")
        xip = next(item for item in layout["regions"] if item["id"] == "rp2350-xip")
        self.assertNotIn("physical", xip)
        for script in base.glob("scripts/memmap_*.ld"):
            declarations = {name: (int(start, 16), int(size) * 1024) for name, start, size in re.findall(
                r"([A-Z_]+)\s*\([^)]*\)\s*:\s*ORIGIN\s*=\s*(0x[0-9a-f]+),\s*LENGTH\s*=\s*(\d+)k",
                script.read_text())}
            with self.subTest(script=script.name):
                self.assertEqual(sram["start"], declarations["RAM"][0])
                for bank, name in zip(resource["banks"], ("RAM", "SCRATCH_X", "SCRATCH_Y")):
                    start, size = declarations[name]
                    self.assertEqual((sram["start"] + bank["offset"], bank["size"]), (start, size))
                self.assertEqual(resource["size"], sum(declarations[name][1] for name in ("RAM", "SCRATCH_X", "SCRATCH_Y")))
                if "FLASH" in declarations:
                    self.assertEqual((xip["start"], xip["size"]), declarations["FLASH"])
        for config in base.glob("configs/*/defconfig"):
            self.assertIn(f"CONFIG_RAM_SIZE={resource['size']}\n", config.read_text())
        self.assertEqual(sram["size"], 520 * 1024)

    def test_descriptor_banks_partition_storage_and_cpu_windows_are_unambiguous(self):
        catalog = read_json(LAYOUTS / "catalog.json")
        for filename in {board["layout"] for board in catalog["boards"]}:
            layout = read_json(LAYOUTS / filename)
            resources = {item["id"]: item for item in layout["physical_resources"]}
            self.assertEqual(len(resources), len(layout["physical_resources"]))
            for resource in resources.values():
                cursor = 0
                for bank in resource["banks"]:
                    self.assertEqual(bank["offset"], cursor)
                    self.assertGreater(bank["size"], 0)
                    cursor += bank["size"]
                self.assertEqual(cursor, resource["size"])
            for domain in ("cpu_virtual", "linked_load"):
                previous = 0
                for region in sorted((r for r in layout["regions"] if r["domain"] == domain), key=lambda r: r["start"]):
                    self.assertGreaterEqual(region["start"], previous)
                    self.assertGreater(region["size"], 0)
                    previous = region["start"] + region["size"]
                    self.assertLessEqual(previous, 1 << 64)
                    if "physical" in region:
                        backing = region["physical"]
                        self.assertGreaterEqual(backing["offset"], 0)
                        self.assertLessEqual(backing["offset"] + region["size"], resources[backing["id"]]["size"])


class RefreshAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.layouts = root / "tool" / "layouts"
        self.references = root / "references"
        shutil.copytree(LAYOUTS, self.layouts)
        shutil.copytree(REFERENCES, self.references)
        (self.layouts.parent / "src").mkdir()
        shutil.copy2(TOOL / "src/layout_catalog.rs", self.layouts.parent / "src/layout_catalog.rs")
        self.manifest = read_json(self.references / "sources.json")

    def save_manifest(self):
        (self.references / "sources.json").write_text(json.dumps(self.manifest))

    def test_changed_file_is_rejected_offline(self):
        item = self.manifest["sources"][0]
        (self.references / item["local"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Pinned source differs"):
            BSP.check(self.layouts, self.references)

    def test_unlisted_file_is_rejected(self):
        (self.references / "sources/extra.h").write_text("unreviewed")
        with self.assertRaisesRegex(ValueError, "unlisted"):
            BSP.check(self.layouts, self.references)

    def test_changed_selector_table_is_rejected(self):
        (self.layouts.parent / "src/layout_catalog.rs").write_text("stale catalogue")
        with self.assertRaisesRegex(ValueError, "Rust layout table differs"):
            BSP.check(self.layouts, self.references)

    def test_download_rejects_unpinned_url_before_requesting_it(self):
        self.manifest["sources"][0]["url"] = "https://example.com/unreviewed.h"
        self.save_manifest()
        with mock.patch.object(BSP.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(ValueError, "Source URL"):
                BSP.download(self.layouts, self.references)
            request.assert_not_called()

    def test_manifest_path_escape_and_symlink_are_rejected(self):
        for value in ("../escape.h", "/tmp/escape.h", "sources/../escape.h", "sources\\escape.h"):
            with self.subTest(path=value), self.assertRaisesRegex(ValueError, "path"):
                BSP.local_path(self.references, value)
        link = self.references / "sources/link.h"
        try:
            link.symlink_to(self.references / self.manifest["sources"][0]["local"])
        except OSError:
            self.skipTest("Symlinks are unavailable for this test account")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            BSP.check(self.layouts, self.references)

    def test_download_checks_all_responses_before_replacing_any_source(self):
        responses = {item["url"]: (self.references / item["local"]).read_bytes() for item in self.manifest["sources"]}
        first, last = self.manifest["sources"][0], self.manifest["sources"][-1]
        changed = self.references / first["local"]
        changed.write_bytes(b"local file awaiting restoration")
        responses[last["url"]] = b"mismatching upstream response"
        with mock.patch.object(BSP.urllib.request, "urlopen", side_effect=lambda request, timeout: io.BytesIO(responses[request.full_url])):
            with self.assertRaisesRegex(ValueError, "Pinned source differs"):
                BSP.download(self.layouts, self.references)
        self.assertEqual(changed.read_bytes(), b"local file awaiting restoration")

    def test_download_restores_exact_source_without_executing_it(self):
        responses = {item["url"]: (self.references / item["local"]).read_bytes() for item in self.manifest["sources"]}
        item = self.manifest["sources"][0]
        (self.references / item["local"]).unlink()
        with mock.patch.object(BSP.urllib.request, "urlopen", side_effect=lambda request, timeout: io.BytesIO(responses[request.full_url])):
            result = BSP.download(self.layouts, self.references)
        self.assertEqual(result["boards"], 5)
        self.assertEqual((self.references / item["local"]).read_bytes(), responses[item["url"]])


if __name__ == "__main__":
    unittest.main()
