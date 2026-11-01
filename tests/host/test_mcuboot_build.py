"""Product wiring stays consistent with the signed image and upstream pins."""
import importlib.util
import os
import re
import runpy
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from platform_config import resolve

SPEC = importlib.util.spec_from_file_location("nuttx_mcuboot", ROOT / "platform/nuttx/mcuboot.py")
BOOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOT)


class BootWiringTests(unittest.TestCase):
    def test_direct_startup_requires_coherent_boot_and_bounded_native_args(self):
        configure = runpy.run_path(str(ROOT / "platform/nuttx/init.py"))["application_config"]
        profile = resolve("esp32s3-camera-stream-startup")
        config = configure(profile, "camera", 100, 32768)
        self.assertEqual(config["NETINIT_THREAD"], "n")
        self.assertEqual(config["INIT_ENTRYPOINT"], '"camera_main"')
        self.assertEqual(config["INIT_STACKSIZE"], "32768")
        self.assertEqual(profile["init"]["args"], [])
        self.assertEqual(config["UART0_SERIAL_CONSOLE"], "y")
        self.assertEqual(config["OTHER_SERIAL_CONSOLE"], "n")
        self.assertEqual(BOOT.application_config(profile)["ESP32S3_RWDT_INHERIT"], "y")
        self.assertEqual(BOOT.watchdog_timeout_ms(profile), 180000)
        storage = BOOT.application_config(profile)
        self.assertEqual(storage["MTD_CONFIG_NVS"], "y")
        self.assertEqual(storage["MTD_CONFIG_NAME_LEN"], "32")
        offset = int(storage["ESP32S3_STORAGE_MTD_OFFSET"], 0)
        size = int(storage["ESP32S3_STORAGE_MTD_SIZE"], 0)
        self.assertEqual(offset, profile["mcuboot"]["scratch-offset"] + profile["mcuboot"]["scratch-size"])
        self.assertEqual(size, 2 * profile["mcuboot"]["erase-size"])
        self.assertLessEqual(offset + size, profile["mcuboot"]["flash-size"])
        for changed in ({"scratch-offset": 0, "scratch-size": 0x1000},
                        {"scratch-offset": 0xfc0000},
                        {"scratch-offset": 0x10000, "scratch-size": 0x10000}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "config partition"):
                BOOT.application_config({**profile, "mcuboot": {**profile["mcuboot"], **changed}})
        self.assertFalse(BOOT.protected(profile))
        self.assertNotEqual(profile["image-class"], resolve("esp32s3-camera-stream-mcuboot")["image-class"])
        self.assertEqual(configure({}, "camera", 100, 32768), {})
        for capability in ("boot", "watchdog"):
            with self.subTest(capability=capability), self.assertRaises(ValueError):
                configure({**profile, "hal": {**profile["hal"], capability: "none"}}, "camera", 100, 32768)
        for args in (None, "boot-tls", [True], ["x"] * 17, ["x" * 129], ['a"b'], ['a\\b'], ["a\nb"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                configure({**profile, "init": {"command": "camera", "args": args}}, "camera", 100, 32768)
        for command, priority, stack in (("other", 100, 32768), ("bad-name", 100, 32768),
                                         ("camera", True, 32768), ("camera", 256, 32768),
                                         ("camera", 100, 0)):
            with self.subTest(command=command, priority=priority, stack=stack), self.assertRaises(ValueError):
                configure(profile, command, priority, stack)

    def test_actual_nuttx_header_preserves_direct_entry_and_argument_boundaries(self):
        configure = runpy.run_path(str(ROOT / "platform/nuttx/init.py"))["application_config"]
        profile = resolve("esp32s3-camera-stream-startup")
        args = ["space test", "a,b", ""]
        profile["init"]["args"] = args
        values = configure(profile, "camera", 100, 32768)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            tools = ROOT / "external/nuttx/tools"
            subprocess.run(["cc", "-I"+str(tools), str(tools/"mkconfig.c"), str(tools/"cfgdefine.c"),
                            "-o", str(work/"mkconfig")], check=True, capture_output=True)
            (work/".config").write_text('CONFIG_INIT_ARGS="old"\nCONFIG_INIT_ENTRYPOINT="old_main"\n')
            backend = (ROOT / "tools/build-nuttx-std-app.sh").read_text()
            writer = backend.split("<<'PY_SET'\n", 1)[1].split("\nPY_SET", 1)[0]
            subprocess.run([sys.executable, "-c", writer, *[f"{key}={value}" for key, value in values.items()]],
                           cwd=work, check=True, capture_output=True)
            validation = 'for required in "${NUTTX_REQUIRE[@]:-}"; do' + backend.split(
                'for required in "${NUTTX_REQUIRE[@]:-}"; do', 1)[1].split("\ndone", 1)[0] + "\ndone\n"
            subprocess.run(["bash", "-c", 'NUTTX_REQUIRE=("$@")\nNXRS_DEPLOYMENT=fixture\n'+validation,
                            "fixture", *[f"CONFIG_{key}={value}" for key, value in values.items()]],
                           cwd=work, check=True, capture_output=True)
            header = subprocess.check_output([str(work/"mkconfig"), str(work)])
            (work/"config.h").write_bytes(header)
            (work/"witness.c").write_text('''#include "config.h"
#include <stdio.h>
static int camera_main(int argc, const char *argv[]) {
  for (int i = 1; i < argc; ++i) puts(argv[i]);
  return 0;
}
int main(void) {
  const char *args[] = {"init",
#ifdef CONFIG_INIT_ARGS
    CONFIG_INIT_ARGS,
#endif
  };
  return CONFIG_INIT_ENTRYPOINT(sizeof(args)/sizeof(args[0]), args);
}
''')
            subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", str(work/"witness.c"),
                            "-o", str(work/"witness")], check=True, capture_output=True)
            self.assertEqual(subprocess.check_output([str(work/"witness")], text=True).splitlines(), args)
            # Empty product args must survive the actual NuttX config writer/header tool.
            profile["init"]["args"] = []
            values = configure(profile, "camera", 100, 32768)
            subprocess.run([sys.executable, "-c", writer, *[f"{key}={value}" for key, value in values.items()]],
                           cwd=work, check=True, capture_output=True)
            (work/"config.h").write_bytes(subprocess.check_output([str(work/"mkconfig"), str(work)]))
            subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", str(work/"witness.c"),
                            "-o", str(work/"witness")], check=True, capture_output=True)
            self.assertEqual(subprocess.check_output([str(work/"witness")], text=True), "")

    @unittest.skipUnless(importlib.util.find_spec("kconfiglib"), "install NuttX kconfiglib for driver selection")
    def test_encrypted_driver_selection_is_independent_of_psram(self):
        import itertools
        import kconfiglib
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()
        names = ("arch/xtensa/src/esp32s3/Kconfig", "arch/xtensa/src/esp32s3/Make.defs",
                 "arch/xtensa/src/common/espressif/Kconfig",
                 "boards/xtensa/esp32s3/common/src/esp32s3_board_spiflash.c")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in names:
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(subprocess.check_output(["git", "-C", str(ROOT / "external/nuttx"),
                                                         "show", f"{revision}:{name}"]))
            for patched in (False, True):
                if patched:
                    subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/nuttx/patches/0098-esp32s3-select-encrypted-flash-driver.patch")],
                                   cwd=directory, check=True, capture_output=True)
                # Use the actual SDK declarations with only unrelated dependencies stubbed.
                text = "".join((directory / name).read_text() for name in (names[0], names[2]))
                inputs = ("ESP32S3_SPIRAM", "ESP32S3_PARTITION_TABLE", "ESP32S3_OTA_PARTITION_ENCRYPT")
                declarations = "".join(f'config {name}\n\tbool "{name}"\n' for name in inputs)
                declarations += "".join(f"config {name}\n\tbool\n" for name in
                                        ("ESP32_PARTITION_TABLE", "MTD", "MTD_BYTE_WRITE", "MTD_PARTITION"))
                for name in ("ESP32S3_SPIFLASH_LEGACY", "ESP32S3_SPIFLASH", "ESP32S3_MTD",
                             "ESPRESSIF_SPIFLASH", "ESPRESSIF_MTD"):
                    match = re.search(rf"(?ms)^config {name}\n.*?(?=^[A-Za-z#]|\Z)", text)
                    if match:
                        declarations += match.group()
                config_path = directory / "Kconfig"
                config_path.write_text(declarations)
                failures = []
                for values in itertools.product((0, 2), repeat=3):
                    config = kconfiglib.Kconfig(str(config_path), warn=False)
                    for name, value in zip(inputs, values):
                        config.syms[name].set_value(value)
                    config.syms["ESP32S3_SPIFLASH"].set_value(2)
                    config.syms["ESP32S3_MTD"].set_value(2)
                    common = not any(values)
                    if any(config.syms[name].tri_value != (2 if common else 0)
                           for name in ("ESPRESSIF_SPIFLASH", "ESPRESSIF_MTD")):
                        failures.append(values)
                self.assertEqual(failures, [] if patched else [(0, 0, 2)])

    def test_shared_geometry_public_root_and_reversible_configuration(self):
        profile = resolve("esp32s3-camera-stream-mcuboot")
        app = BOOT.application_config(profile)
        text = BOOT.bootloader_config(profile, "/private/tmp/test-root.pem")
        config = dict(line.split("=", 1) for line in text.splitlines())
        for application, boot in (("ESP32S3_OTA_PRIMARY_SLOT_OFFSET", "CONFIG_ESP_IMAGE0_PRIMARY_START_ADDRESS"),
                                  ("ESP32S3_OTA_SECONDARY_SLOT_OFFSET", "CONFIG_ESP_IMAGE0_SECONDARY_START_ADDRESS"),
                                  ("ESP32S3_OTA_SLOT_SIZE", "CONFIG_ESP_APPLICATION_SIZE"),
                                  ("ESP32S3_OTA_SCRATCH_OFFSET", "CONFIG_ESP_SCRATCH_OFFSET")):
            self.assertEqual(app[application], config[boot])
        self.assertEqual(app["BOOT_MCUBOOT"], "y")
        self.assertEqual(app["ESP32S3_SPIFLASH_AUTO_BRINGUP"], "n")
        self.assertEqual(app["MCUBOOT_VERSION"], app["ESP32S3_MCUBOOT_VERSION"])
        self.assertEqual(int(app["ESP32S3_STORAGE_MTD_OFFSET"], 0), 0x460000)
        self.assertEqual(config["CONFIG_ESP_SIGN_EC256"], "1")
        self.assertEqual(config["CONFIG_ESP_DOWNGRADE_PREVENTION"], "1")
        self.assertEqual(config["CONFIG_ESP_DOWNGRADE_PREVENTION_SECURITY_COUNTER"], "1")
        self.assertEqual(config["MCUBOOT_UUID_CID"], "1")
        self.assertEqual(bytes(int(byte, 16) for byte in config["CONFIG_ESP_IMAGE_CLASS_ID"].split(",")),
                         UUID(profile["image-class"]).bytes)
        self.assertEqual(config["CONFIG_BOOTLOADER_WDT_ENABLE"], "1")
        self.assertEqual(config["CONFIG_BOOTLOADER_WDT_TIME_MS"], "9000")
        self.assertFalse(any(key.startswith("CONFIG_SECURE_") for key in config))
        self.assertIn(BOOT.MCUBOOT_REVISION, (ROOT / "tools/mcuboot-requirements.txt").read_text())

    def test_startup_watchdog_budget_is_a_bounded_product_setting(self):
        profile = resolve("esp32s3-camera-stream-mcuboot")
        for value in (1, 180000, 300000):
            config = BOOT.bootloader_config({**profile, "boot-watchdog-ms": value}, "/tmp/root.pem")
            self.assertIn(f"CONFIG_BOOTLOADER_WDT_TIME_MS={value}\n", config)
        for value in (True, False, 0, -1, 300001, "180000", 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BOOT.bootloader_config({**profile, "boot-watchdog-ms": value}, "/tmp/root.pem")

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned MCUboot source for watchdog header regression")
    def test_upstream_header_preserves_the_configured_watchdog_budget(self):
        relative = "boot/espressif/hal/include/esp32s3/sdkconfig.h"
        patch = ROOT / "upstream/mcuboot/patches/0007-espressif-watchdog-budget.patch"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text((Path(os.environ["NXRS_MCUBOOT_SOURCE"]) / relative).read_text())
            reverse = ["patch", "--batch", "--force", "-R", "-p1", "-i", str(patch)]
            if subprocess.run([*reverse, "--dry-run"], cwd=directory, capture_output=True).returncode == 0:
                subprocess.run(reverse, cwd=directory, check=True, capture_output=True)
            original = path.read_text()
            subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=directory,
                           check=True, capture_output=True)
            for source, override, expected in ((original, None, 9000), (original, 180000, None),
                                               (path.read_text(), None, 9000),
                                               (path.read_text(), 180000, 180000)):
                header = directory / "config.h"
                header.write_text(source)
                result = subprocess.run(["cc", "-E", "-P", "-Werror", "-include", str(header),
                                         *([f"-DCONFIG_BOOTLOADER_WDT_TIME_MS={override}"] if override else []),
                                         "-x", "c", "-"], input="CONFIG_BOOTLOADER_WDT_TIME_MS\n",
                                        text=True, capture_output=True)
                if expected is None:
                    self.assertNotEqual(result.returncode, 0, "baseline must reject the forced redefinition")
                    self.assertIn("CONFIG_BOOTLOADER_WDT_TIME_MS", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(int(result.stdout.strip()), expected)

    def test_protected_build_matches_encryption_alignment_and_blocks_runtime_updates(self):
        profile = resolve("esp32s3-camera-stream-protected-build")
        app = BOOT.application_config(profile)
        text = BOOT.bootloader_config(profile, "/tmp/public-root.pem")
        self.assertEqual(profile["mcuboot"]["write-align"], 32)
        self.assertEqual(app["ESP32S3_OTA_PARTITION_ENCRYPT"], "y")
        self.assertEqual(app["ESP32S3_SPIFLASH_MTD_BLKSIZE"], "32")
        self.assertNotIn("BOOT_MCUBOOT", app)
        self.assertNotEqual(profile["image-class"], resolve("esp32s3-camera-stream-mcuboot")["image-class"])
        for key in ("SECURE_BOOT_V2_ENABLED", "SECURE_FLASH_ENC_ENABLED",
                    "SECURE_BOOT_REQUIRE_ALREADY_ENABLED", "EFUSE_WRITES_FORBIDDEN",
                    "SECURE_FLASH_REQUIRE_ALREADY_ENABLED", "SECURE_FLASH_ENCRYPTION_MODE_RELEASE"):
            self.assertIn(f"CONFIG_{key}=1\n", text)
        for overrides in ({"hardware-protection": "yes"},
                          {"mcuboot": {**profile["mcuboot"], "write-align": 4}},
                          {"hal": {**profile["hal"], "boot": "nuttx"}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                BOOT.application_config({**profile, **overrides})

    def test_unsupported_target_geometry_and_config_injection_fail(self):
        profile = resolve("esp32s3-camera-stream-mcuboot")
        for field, value in (("slot-size", 0x300000), ("primary-offset", 0x10000),
                             ("write-align", 32)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                BOOT.application_config({**profile, "mcuboot": {**profile["mcuboot"], field: value}})
        with self.assertRaises(ValueError):
            BOOT.application_config({**profile, "target": "thumbv8m.main-nuttx-eabi"})
        for path in ("relative.pem", "/tmp/root\nCONFIG_SECURE_BOOT=1", "/tmp/root;injected"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                BOOT.bootloader_config(profile, path)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for counter regression")
    def test_upstream_counter_policy_preserves_equal_counter_recovery(self):
        source = (Path(os.environ["NXRS_MCUBOOT_SOURCE"]) / "boot/bootutil/src/loader.c").read_text()
        function = source[source.index("static int\ncheck_downgrade_prevention("):]
        function = function[:function.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "counter.inc").write_text(function)
            executable = directory / "counter"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                            str(ROOT / "tests/host/mcuboot_security_counter.c"), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for image-class regression")
    def test_native_image_class_matches_only_the_configured_product(self):
        sdk = Path(os.environ["NXRS_MCUBOOT_SOURCE"])
        relative = "boot/espressif/port/esp_mcuboot.c"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_bytes((sdk / relative).read_bytes())
            if "fih_ret boot_uuid_cid_match(" not in path.read_text():
                subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/mcuboot/patches/0006-espressif-image-class.patch")],
                               cwd=directory, check=True, capture_output=True)
            source = path.read_text().split("fih_ret boot_uuid_cid_match(", 1)[1]
            (directory / "class.inc").write_text("fih_ret boot_uuid_cid_match(" + source[:source.index("\n}\n") + 3])
            config = directory / "mcuboot_config/mcuboot_config.h"
            config.parent.mkdir()
            expected = UUID(resolve("esp32s3-camera-stream-mcuboot")["image-class"]).bytes
            config.write_text("#define CONFIG_ESP_IMAGE_CLASS_ID " +
                              ",".join(f"0x{byte:02x}" for byte in expected) + "\n")
            executable = directory / "class"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                            "-I", str(sdk / "boot/bootutil/include"),
                            str(ROOT / "tests/host/mcuboot_image_class.c"),
                            str(sdk / "boot/bootutil/src/fault_injection_hardening.c"),
                            "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True, timeout=5)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE") and os.environ.get("NXRS_BOOT_HAL_SOURCE"),
                         "set pinned boot SDK exports for provisioning regression")
    def test_protected_startup_and_native_burn_refuse_provisioning(self):
        inputs = (("mcuboot", Path(os.environ["NXRS_MCUBOOT_SOURCE"]), "boot/espressif/main.c",
                   "0002-require-provisioned-protection.patch"),
                  ("esp-hal-3rdparty", Path(os.environ["NXRS_BOOT_HAL_SOURCE"]),
                   "components/efuse/esp32s3/esp_efuse_utility.c", "0004-forbid-efuse-programming.patch"))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sources = []
            for component, sdk, relative, patch in inputs:
                path = directory / component / relative
                path.parent.mkdir(parents=True)
                original = (sdk / relative).read_text()
                path.write_text(original)
                subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream" / component / "patches" / patch)],
                               cwd=directory / component, check=True, capture_output=True)
                sources.append((original, path.read_text()))
            burn = sources[1][1]
            burn = burn[burn.index("esp_err_t esp_efuse_utility_burn_chip(void)"):]
            (directory / "burn.inc").write_text(burn[:burn.index("\n}\n") + 3])
            for source, expected in ((sources[0][0], 1), (sources[0][1], 0)):
                prefix = source[source.index("int main()") : source.index("    /* Rough steps")]
                (directory / "startup.inc").write_text(prefix.replace("int main()", "int boot_entry(void)") + "return 0;\n}\n")
                executable = directory / "guard"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                str(ROOT / "tests/host/mcuboot_provisioning_guard.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for native FTL regression")
    def test_trailer_writes_never_erase_the_sector(self):
        sdk = Path(os.environ["NXRS_MCUBOOT_SOURCE"])
        relative = "boot/nuttx/src/flash_map_backend/flash_map_backend.c"
        original = (sdk / relative).read_text()
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"],
                                           cwd=ROOT, text=True).strip()
        ftl = subprocess.check_output(
            ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:drivers/mtd/ftl.c"], text=True)
        function = ftl[ftl.rindex("static ssize_t ftl_flush_direct("):]
        function = function[:function.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text(original)
            subprocess.run(["patch", "-p1", "-i", str(ROOT / "upstream/mcuboot/patches/0001-nuttx-direct-trailer-writes.patch")],
                           cwd=directory, check=True, capture_output=True)
            (directory / "ftl.inc").write_text(function)
            erase = path.read_text()
            erase = erase[erase.index("int flash_area_erase("):]
            (directory / "erase.inc").write_text(erase[:erase.index("\n}\n") + 3])
            executable = directory / "erase"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                            str(ROOT / "tests/host/mcuboot_native_erase.c"), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True, timeout=5)
            for source, expected in ((original, 1), (path.read_text(), 0)):
                flags = re.search(r"fd = open\(dev->fa_cfg->fa_mtd_path, (.*?)\);", source).group(1)
                (directory / "open_flags.inc").write_text(f"#define SDK_OPEN_FLAGS ({flags})\n")
                executable = directory / "trailer"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                str(ROOT / "tests/host/mcuboot_trailer_nor.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for staging I/O regression")
    def test_native_staging_rejects_short_io_and_reserves_matching_metadata(self):
        relative = "boot/nuttx/src/flash_map_backend/flash_map_backend.c"
        header = "boot/nuttx/include/mcuboot_config/mcuboot_config.h"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in (relative, header):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((Path(os.environ["NXRS_MCUBOOT_SOURCE"]) / name).read_bytes())
            for patch in ("0001-nuttx-direct-trailer-writes.patch", "0004-nuttx-encrypted-metadata-alignment.patch"):
                subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/mcuboot/patches" / patch)],
                               cwd=directory, check=True, capture_output=True)
            for patched in (False, True):
                if patched:
                    subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/mcuboot/patches/0005-nuttx-bounded-image-io.patch")],
                                   cwd=directory, check=True, capture_output=True)
                source = (directory / relative).read_text()
                functions = []
                for name in ("flash_area_read", "flash_area_write", "flash_area_align", "flash_area_get_sector"):
                    match = re.search(r"(?:int|uint32_t) " + name + r"\(", source)
                    function = source[match.start():]
                    functions.append(function[:function.index("\n}\n") + 3])
                (directory / "io.inc").write_text("\n".join(functions))
                for flags in ([], ["-DCONFIG_ESP32S3_APP_FORMAT_MCUBOOT"], ["-DCONFIG_ESP32S3_OTA_PARTITION_ENCRYPT"]):
                    executable = directory / "io"
                    subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-Wno-unused-variable", "-I", str(directory),
                                    *flags, str(ROOT / "tests/host/mcuboot_staging_io.c"), "-o", str(executable)], check=True)
                    self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, 0 if patched else 1)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for encrypted trailer regression")
    def test_protected_native_trailer_fields_use_independent_program_units(self):
        sdk = Path(os.environ["NXRS_MCUBOOT_SOURCE"])
        backend = "boot/nuttx/src/flash_map_backend/flash_map_backend.c"
        header = "boot/nuttx/include/mcuboot_config/mcuboot_config.h"
        writer = (sdk / "boot/bootutil/src/bootutil_public.c").read_text()
        writer = writer[writer.index("int\nboot_write_trailer("):]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for relative in (backend, header):
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((sdk / relative).read_text())
            config = directory / "nuttx/config.h"
            config.parent.mkdir()
            (directory / "trailer.inc").write_text(writer[:writer.index("\n}\n") + 3])
            for patched in (False, True):
                if patched:
                    subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/mcuboot/patches/0004-nuttx-encrypted-metadata-alignment.patch")],
                                   cwd=directory, check=True, capture_output=True)
                align = (directory / backend).read_text().split("uint32_t flash_area_align(", 1)[1]
                (directory / "alignment.inc").write_text("uint32_t flash_area_align(" + align[:align.index("\n}\n") + 3])
                config.write_text("#define CONFIG_ESP32S3_OTA_PARTITION_ENCRYPT 1\n#define CONFIG_ESP32S3_SPIFLASH_MTD_BLKSIZE 32\n")
                command = ["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                           "-I", str(directory / "boot/nuttx/include"),
                           str(ROOT / "tests/host/mcuboot_encrypted_trailer.c"), "-o", str(directory / "trailer")]
                subprocess.run(command, check=True)
                self.assertEqual(subprocess.run([str(directory / "trailer")]).returncode, 0 if patched else 1)
            config.write_text("#define CONFIG_ESP32S3_OTA_PARTITION_ENCRYPT 1\n#define CONFIG_ESP32S3_SPIFLASH_MTD_BLKSIZE 256\n")
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Encrypted MCUboot requires 32-byte MTD blocks", failed.stderr)

    def test_encrypted_program_failure_propagates_and_releases_hardware(self):
        relative = "arch/xtensa/src/esp32s3/esp32s3_spiflash_mtd.c"
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"],
                                           cwd=ROOT, text=True).strip()
        original = subprocess.check_output(
            ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{relative}"], text=True)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text(original)
            subprocess.run(["patch", "-p1", "-i", str(ROOT / "upstream/nuttx/patches/0096-esp32s3-propagate-encrypted-write-errors.patch")],
                           cwd=directory, check=True, capture_output=True)
            for source, expected in ((original, 1), (path.read_text(), 0)):
                function = source[source.index("int spi_flash_write_encrypted_manu("):]
                (directory / "encrypted_write.inc").write_text(function[:function.index("\n}\n") + 3])
                executable = directory / "write"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                str(ROOT / "tests/host/esp32s3_encrypted_write.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)

    @unittest.skipUnless(os.environ.get("NXRS_MCUBOOT_SOURCE"), "set pinned SDK source for encrypted flash regression")
    def test_encrypted_boot_and_native_flash_share_nor_contract(self):
        relative = "arch/xtensa/src/esp32s3/esp32s3_spiflash_mtd.c"
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True).strip()
        native = subprocess.check_output(["git", "-C", str(ROOT / "external/nuttx"), "show",
                                          f"{revision}:{relative}"], text=True)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for source, name, patches in (
                (Path(os.environ["NXRS_MCUBOOT_SOURCE"]) / "boot/espressif/port/esp_mcuboot.c",
                 "boot/espressif/port/esp_mcuboot.c", ("mcuboot/0003-encrypted-write-once-flash.patch",)),
                (None, relative, ("nuttx/0096-esp32s3-propagate-encrypted-write-errors.patch",
                                  "nuttx/0097-esp32s3-encrypted-flash-contract.patch"))):
                path = directory / name
                path.parent.mkdir(parents=True)
                path.write_text(source.read_text() if source else native)
                for patch in patches:
                    component, filename = patch.split("/", 1)
                    subprocess.run(["patch", "--batch", "-p1", "-i",
                                    str(ROOT / "upstream" / component / "patches" / filename)],
                                   cwd=directory, check=True, capture_output=True)
            boot = (directory / "boot/espressif/port/esp_mcuboot.c").read_text()
            native = (directory / relative).read_text()
            (directory / "boot_io.inc").write_text(boot[boot.index("static bool aligned_flash_read("):
                                                        boot.index("uint32_t flash_area_align(")])
            for name, marker in (("native_read", "static int spi_flash_read_erased_aware("),
                                 ("native_write", "int spi_flash_write_encrypted_manu(")):
                function = native[native.index(marker):]
                (directory / f"{name}.inc").write_text(function[:function.index("\n}\n") + 3])
            for patched in (False, True):
                if patched:
                    subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/nuttx/patches/0099-esp32s3-encrypted-write-alignment.patch")],
                                   cwd=directory, check=True, capture_output=True)
                native = (directory / relative).read_text()
                function = native[native.index("static inline bool spi_flash_encrypt_ll_check("):]
                constant = re.search(r"^#define SPI_FLASH_ENCRYPT_MIN_SIZE.*$", native, re.M).group()
                (directory / "native_check.inc").write_text(constant + "\n" + function[:function.index("\n}\n") + 3])
                executable = directory / "io"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                str(ROOT / "tests/host/mcuboot_encrypted_io.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, 0 if patched else 1)

    @unittest.skipUnless(os.environ.get("NXRS_BOOT_HAL_SOURCE"), "set pinned SDK source for watchdog clock regression")
    def test_sdk_clock_init_keeps_the_inherited_deadline_and_clock(self):
        relative = "components/esp_system/port/soc/esp32s3/clk.c"
        name = "0005-esp32s3-inherit-watchdog-clock.patch"
        self.assertIn(name, runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["esp-hal-3rdparty"])
        original = (Path(os.environ["NXRS_BOOT_HAL_SOURCE"]) / relative).read_text()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text(original)
            subprocess.run(["patch", "--batch", "-p1", "-i", str(ROOT / "upstream/esp-hal-3rdparty/patches" / name)],
                           cwd=directory, check=True, capture_output=True)
            for source, inherited, cycles, expected in ((original, True, 1024, 1), (path.read_text(), False, 1024, 0),
                                                         (path.read_text(), True, 1024, 0), (path.read_text(), True, 0, 0)):
                prefix = source[source.index("__attribute__((weak)) void esp_clk_init(void)"):]
                prefix = prefix[:prefix.index("\n    rtc_cpu_freq_config_t")] + "\n}\n"
                (directory / "clock.c").write_text(prefix)
                executable = directory / "clock"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                f"-DSLOW_CLK_CAL_CYCLES={cycles}",
                                *(["-DCONFIG_ESP32S3_RWDT_INHERIT=1"] if inherited else []),
                                str(ROOT / "tests/host/esp32s3_wdt_clock.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)
                if inherited and expected == 0:
                    self.assertLess(subprocess.run([str(executable), "failed-calibration"], timeout=5).returncode, 0)

    @unittest.skipUnless(os.environ.get("NXRS_APPLICATION_HAL_SOURCE"), "set native application SDK for late-watchdog regression")
    def test_sdk_late_startup_keeps_the_user_owned_watchdog(self):
        sdk = Path(os.environ["NXRS_APPLICATION_HAL_SOURCE"])
        relative = "nuttx/esp32s3/include/sdkconfig.h"
        name = "0006-esp32s3-inherit-watchdog-user-handoff.patch"
        patch = ROOT / "upstream/esp-hal-3rdparty/patches" / name
        self.assertIn(name, runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["esp-hal-3rdparty"])
        startup = (sdk / "components/esp_system/startup_funcs.c").read_text()
        hook = startup[startup.index("#ifndef CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE"):]
        hook = hook[:hook.index("#endif // CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE")] + "#endif\n"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text((sdk / relative).read_text())
            # CI supplies its patched build export; restore the exact preimage privately.
            reverse = ["patch", "--batch", "--force", "-R", "-p1", "-i", str(patch)]
            if subprocess.run([*reverse, "--dry-run"], cwd=directory, capture_output=True).returncode == 0:
                subprocess.run(reverse, cwd=directory, check=True, capture_output=True)
            original = path.read_text()
            subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=directory, check=True, capture_output=True)
            (directory / "hook.inc").write_text(hook)
            (directory / "nuttx").mkdir(exist_ok=True)
            (directory / "nuttx/config.h").write_text("")
            for source, inherited, expected in ((original, True, 1), (path.read_text(), False, 0),
                                                 (path.read_text(), True, 0)):
                config = source[source.index("#include <nuttx/config.h>"):]
                (directory / "config.inc").write_text(config[:config.index("#define CONFIG_ESP_TIMER_TASK_AFFINITY")])
                executable = directory / "startup"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                *(["-DCONFIG_ESP32S3_RWDT_INHERIT=1"] if inherited else []),
                                str(ROOT / "tests/host/esp32s3_wdt_startup.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)

    def test_actual_nuttx_handoff_preserves_bits_and_unlocks_then_relocks(self):
        relative = "arch/xtensa/src/esp32s3/esp32s3_wdt.c"
        revision = subprocess.check_output(["git", "rev-parse", ":external/nuttx"],
                                           cwd=ROOT, text=True).strip()
        original = subprocess.check_output(
            ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{relative}"], text=True)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / relative
            path.parent.mkdir(parents=True)
            path.write_text(original)
            kconfig = "arch/xtensa/src/esp32s3/Kconfig"
            (directory / kconfig).write_text(subprocess.check_output(
                ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{kconfig}"], text=True))
            for name in ("0094-esp32s3-boot-watchdog-handoff.patch", "0100-esp32s3-inherit-boot-watchdog.patch"):
                self.assertIn(name, runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["nuttx"])
                subprocess.run(["patch", "-p1", "-i", str(ROOT / "upstream/nuttx/patches" / name)],
                               cwd=directory, check=True, capture_output=True)
            for source, inherited, expected in ((original, False, 1), (path.read_text(), False, 0),
                                                 (path.read_text(), True, 0)):
                function = source[source.index("void esp32s3_wdt_early_deinit(void)"):]
                function = function[:function.index("\n}") + 2]
                (directory / "early.c").write_text(function)
                executable = directory / "handoff"
                subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(directory),
                                *(["-DCONFIG_ESP32S3_RWDT_INHERIT=1"] if inherited else []),
                                str(ROOT / "tests/host/esp32s3_wdt_handoff.c"), "-o", str(executable)], check=True)
                self.assertEqual(subprocess.run([str(executable)], timeout=5).returncode, expected)


@unittest.skipUnless(importlib.util.find_spec("espsecure"), "install esptool==4.8.1 for ROM signature tests")
class RomSigningTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("imgtool"), "install tools/mcuboot-requirements.txt")
    def test_factory_ciphertext_initialization_and_refusals(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from imgtool.keys import load
        from mcuboot_image import sign
        import hashlib
        import json
        modules = []
        for name in ("build-mcuboot", "prepare-protected-factory"):
            spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
        builder, factory = modules
        profile = resolve("esp32s3-camera-stream-protected-build")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key, root = directory / "rom.key", directory / "rom.pem"
            material = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            key.write_bytes(material.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
            root.write_bytes(material.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            app_key, app_root = directory / "app.key", directory / "app.pem"
            subprocess.run(["imgtool", "keygen", "-k", str(app_key), "-t", "ecdsa-p256"], check=True)
            app_root.write_bytes(load(str(app_key)).get_public_pem())
            source, boot, image = directory / "source.bin", directory / "bootloader.signed.bin", directory / "factory.bin"
            source.write_bytes(b"test firmware" * 128)
            builder.sign_rom(source, boot, key, root)
            sign(source, image, profile["mcuboot"], app_key, app_root, "1.2.3", 7,
                 class_id=profile["image-class"], confirm=True)
            metadata = {"mcuboot": BOOT.MCUBOOT_REVISION, "esp-hal-3rdparty": BOOT.HAL_REVISION,
                        "platform": profile["name"], "layout": profile["mcuboot"],
                        "image_class": profile["image-class"],
                        "bootloader_file": boot.name, "hardware_secure_boot": True, "flash_encryption": True,
                        "efuse_writes_forbidden": True, "encrypted_write_once": True,
                        "rom_root_pem_sha256": hashlib.sha256(root.read_bytes()).hexdigest(),
                        "public_root_sha256": hashlib.sha256(app_root.read_bytes()).hexdigest(),
                        "bootloader_sha256": hashlib.sha256(boot.read_bytes()).hexdigest()}
            (directory / "bootloader.json").write_text(json.dumps(metadata))
            flash_key = directory / "flash.key"
            flash_key.write_bytes(os.urandom(32))
            output = directory / "bundle"
            factory.prepare(profile, directory, image, root, app_root, flash_key, output)
            manifest = json.loads((output / "factory.json").read_text())
            self.assertFalse(manifest["hardware_qualified"])
            self.assertFalse(manifest["runtime_updates"])
            self.assertTrue(manifest["efuse_writes_forbidden"])
            self.assertTrue(manifest["encrypted_write_once"])
            self.assertEqual(manifest["image_class"], profile["image-class"])
            self.assertEqual([region["offset"] for region in manifest["regions"]],
                             [0, 0x20000, 0x220000, 0x420000])
            self.assertEqual({path.name for path in output.iterdir()},
                             {"factory.json", "boot.enc.bin", "primary.enc.bin", "secondary.enc.bin", "scratch.enc.bin"})
            for region in manifest["regions"]:
                encrypted, decrypted = output / region["file"], directory / "decoded.bin"
                subprocess.run([sys.executable, "-m", "espsecure", "decrypt_flash_data", "--aes_xts",
                                "--keyfile", str(flash_key), "--address", hex(region["offset"]),
                                "--output", str(decrypted), str(encrypted)], check=True)
                expected = boot.read_bytes() if region["offset"] == 0 else image.read_bytes() if (
                    region["offset"] == profile["mcuboot"]["primary-offset"]) else b"\xff" * region["size"]
                data = bytearray(decrypted.read_bytes())
                ciphertext = encrypted.read_bytes()
                for position in range(0, len(data), 16):
                    if ciphertext[position:position + 16] == b"\xff" * 16:
                        data[position:position + 16] = b"\xff" * 16
                self.assertEqual(data, expected)
                if region["offset"] in (0x220000, 0x420000):
                    self.assertEqual(ciphertext, b"\xff" * region["size"])
                if region["offset"] == 0x20000:
                    self.assertNotEqual(ciphertext[32:48], b"\xff" * 16)
                    self.assertNotEqual(ciphertext[4096:4112], b"\xff" * 16)  # Image padding stays encrypted.
                    self.assertEqual(ciphertext[-96:-64], b"\xff" * 32)
                self.assertEqual(hashlib.sha256(encrypted.read_bytes()).hexdigest(), region["sha256"])
            for name, changed in (("existing", {}), ("private-root", {"rom_root": key}),
                                  ("private-app-root", {"app_root": app_key}),
                                  ("wrong-profile", {"profile": resolve("esp32s3-camera-stream-mcuboot")})):
                target = output if name == "existing" else directory / name
                arguments = dict(profile=profile, boot_build=directory, application=image, rom_root=root,
                                 app_root=app_root, flash_key=flash_key, output=target)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    factory.prepare(**{**arguments, **changed})
                self.assertEqual(target.exists(), name == "existing")
            good = image.read_bytes()
            for name, class_id in (("missing-class", None),
                                   ("wrong-class", resolve("esp32s3-camera-stream-mcuboot")["image-class"])):
                sign(source, image, profile["mcuboot"], app_key, app_root, "1.2.3", 7,
                     class_id=class_id, confirm=True)
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "image class"):
                    factory.prepare(profile, directory, image, root, app_root, flash_key, directory / name)
                self.assertFalse((directory / name).exists())
            subprocess.run(["imgtool", "sign", "-k", str(app_key), "-v", "1.2.3",
                            "-H", "32", "--pad-header", "-S", "0x200000", "--align", "32",
                            "--max-align", "32", "--max-sectors", "512", "--pad", "--confirm",
                            str(source), str(image)], check=True)  # Valid legacy image, no counter.
            with self.assertRaisesRegex(ValueError, "protected 32-bit security counter"):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "counterless")
            self.assertFalse((directory / "counterless").exists())
            data = bytearray(good)
            data[64] ^= 1
            image.write_bytes(data)
            with self.assertRaises(subprocess.CalledProcessError):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "corrupt")
            self.assertFalse((directory / "corrupt").exists())
            data = bytearray(good)
            data[-64] = 0xff  # Remove image_ok without changing the authenticated payload.
            image.write_bytes(data)
            with self.assertRaises(ValueError):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "unconfirmed")
            self.assertFalse((directory / "unconfirmed").exists())
            image.write_bytes(good)
            metadata["encrypted_write_once"] = False
            (directory / "bootloader.json").write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "legacy-format")
            self.assertFalse((directory / "legacy-format").exists())
            metadata["encrypted_write_once"] = True
            metadata["efuse_writes_forbidden"] = False
            (directory / "bootloader.json").write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                factory.prepare(profile, directory, image, root, app_root, flash_key, directory / "unsafe-build")
            self.assertFalse((directory / "unsafe-build").exists())
            self.assertFalse(list(directory.glob(".factory-*")))

    def test_independent_root_corruption_size_and_atomic_publication(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        spec = importlib.util.spec_from_file_location("boot_builder", ROOT / "tools/build-mcuboot.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            keys = [rsa.generate_private_key(public_exponent=65537, key_size=3072) for _ in range(2)]
            key, root, wrong = (directory / name for name in ("key.pem", "root.pem", "wrong.pem"))
            private = keys[0].private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
            key.write_bytes(private)
            for path, material in ((root, keys[0]), (wrong, keys[1])):
                path.write_bytes(material.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            source, output = directory / "unsigned.bin", directory / "signed.bin"
            source.write_bytes(b"payload" * 512)
            builder.sign_rom(source, output, key, root)
            good = output.read_bytes()
            for expected, size in ((wrong, 4096), (root, 0xf000)):
                source.write_bytes(b"x" * size)
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    builder.sign_rom(source, output, key, expected)
                self.assertEqual(output.read_bytes(), good)
            for protected in (source, key, root):
                with self.assertRaises(ValueError):
                    builder.sign_rom(source, protected, key, root)
            with self.assertRaises(ValueError):
                builder.sign_rom(source, output, key, key)
            corrupt = bytearray(good)
            corrupt[64] ^= 1
            output.write_bytes(corrupt)
            result = subprocess.run([sys.executable, "-m", "espsecure", "verify_signature", "--version", "2",
                                     "--keyfile", str(root), str(output)], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(key.read_bytes(), private)
            self.assertFalse(list(directory.glob(".rom-sign-*")))


if __name__ == "__main__":
    unittest.main()
