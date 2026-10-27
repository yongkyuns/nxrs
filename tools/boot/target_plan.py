"""Product boot policy/configuration wiring; no hardware or signing-key operations."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import platform_config


def require(value):
    if not value:
        raise ValueError("boot build policy validation failed")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def select(name, root=ROOT):
    data = platform_config.resolve(name, root)
    platform_config.validate_target(data, "nuttx")
    require(data["target"] == "xtensa-esp32s3-nuttx" and data["image"] == "nuttx.hex")
    boot = data.get("boot")
    require(isinstance(boot, dict) and set(boot) == set(
        "schema format scope chip flash_mode flash_frequency console_uart bootloader_bytes layout".split()))
    require(type(boot["schema"]) is int and boot["schema"] == 1
            and boot["format"] == "mcuboot-rsa3072-pss-sha256"
            and boot["scope"] == "qualification-only" and boot["chip"] == "esp32s3")
    require(boot["flash_mode"] == "dio" and boot["flash_frequency"] == "40m"
            and type(boot["console_uart"]) is int and boot["console_uart"] == 0
            and type(boot["bootloader_bytes"]) is int and boot["bootloader_bytes"] == 0xF000)
    import sign
    sign.validate_layout(boot["layout"])
    require(boot["layout"]["flash_bytes"] == 16 * 1024 * 1024
            and boot["layout"]["max_sectors"] == 512
            and boot["layout"]["primary_offset"] >= 0x10000)
    config = data["kconfig"]
    enabled = set(config["enable"])
    disabled = set(config["disable"])
    require({"INTELHEX_BINARY", "ESP32S3_APP_FORMAT_MCUBOOT", "ESP32S3_ESPTOOL_TARGET_SECONDARY",
             "ESP32S3_FLASH_MODE_DIO", "ESP32S3_FLASH_FREQ_40M", "ESPRESSIF_FLASH_MODE_DIO",
             "ESPRESSIF_FLASH_FREQ_40M"} <= enabled)
    require({"RAW_BINARY", "ESPRESSIF_SIMPLE_BOOT", "ESP32S3_APP_FORMAT_LEGACY", "ESP32S3_MERGE_BINS"} <= disabled)
    require(not enabled.intersection(disabled))
    expected = native_settings(boot)
    assignments = {}
    for item in config["set"]:
        key, value = item.split("=", 1)
        require(key not in assignments)
        assignments[key] = value
    for key, value in expected.items():
        require(key in assignments and int(assignments[key], 0) == value)
    require('CONFIG_ESP32S3_APP_FORMAT_MCUBOOT=y' in config["require"])
    base = json.loads((root / "security/boot-dependencies.json").read_text())
    require(assignments.get("ESP32S3_MCUBOOT_VERSION") == json.dumps(base["mcuboot"]["commit"]))
    return data


def native_settings(boot):
    layout = boot["layout"]
    return {"ESP32S3_OTA_PRIMARY_SLOT_OFFSET": layout["primary_offset"],
            "ESP32S3_OTA_SECONDARY_SLOT_OFFSET": layout["secondary_offset"],
            "ESP32S3_OTA_SLOT_SIZE": layout["slot_bytes"],
            "ESP32S3_OTA_SCRATCH_OFFSET": layout["scratch_offset"],
            "ESP32S3_OTA_SCRATCH_SIZE": layout["scratch_bytes"],
            "ESP32S3_APP_MCUBOOT_HEADER_SIZE": layout["header_bytes"]}


def plan(name, root=ROOT):
    data = select(name, root)
    # Application code/version and local paths are separate release evidence.
    # This stable configuration identity governs a product configuration epoch.
    configuration = {key: data[key] for key in ("name", "board", "target", "hal", "kconfig", "boot")}
    sources = [{"path": str(Path(path).relative_to(root)),
                "sha256": digest(Path(path).read_bytes())} for path in data["sources"]]
    dependencies = json.loads((root / "security/esp32s3-boot-dependencies.json").read_text())
    base = json.loads((root / "security/boot-dependencies.json").read_text())
    settings = {"schema": 1, "target": name, "format": data["boot"]["format"],
                "configuration_sha256": digest(canonical(configuration)), "layout": data["boot"]["layout"]}
    return {"schema": 1, "scope": "qualification-only", "configuration": configuration,
            "settings": settings, "profile_sources": sources,
            "sources": {"mcuboot": base["mcuboot"], "mbedtls": base["mbedtls"], "hal": dependencies["hal"]},
            "rom_authenticated": False, "protected_device_counter": False,
            "physical_qualified": False, "deployment_permitted": False}


def checked_policy(name, path, pin, root=ROOT):
    import sign
    data = sign.untrusted_read(path, 8192)
    policy, public = sign.public_policy(data, pin)
    expected = plan(name, root)
    require(policy["settings"] == expected["settings"])
    return expected, data, public


def fresh(path):
    path = Path(path).absolute()
    require(path.name not in ("", ".", "..") and ".." not in path.parts)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    path = parent / path.name
    require(re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)))
    path.mkdir(mode=0o700)
    return path


def emit(name, output, root=ROOT):
    data = plan(name, root)
    output = fresh(output)
    for filename, value in (("boot-plan.json", data), ("signer-settings.json", data["settings"])):
        with (output / filename).open("xb") as stream:
            stream.write(canonical(value) + b"\n")
    return data


def bootloader_config(data, public_key):
    boot = data["configuration"]["boot"]
    layout = boot["layout"]
    require(re.fullmatch(r"/[A-Za-z0-9_./-]+", str(public_key)))
    values = {"NON_OS_BUILD": 1, "CONFIG_ESPRESSIF_BOOTLOADER_MCUBOOT": 1,
        "CONFIG_ESPTOOLPY_FLASHSIZE_16MB": 1, "CONFIG_ESP_FLASH_SIZE": "16MB",
        "CONFIG_ESPTOOLPY_FLASHMODE_DIO": 1, "CONFIG_ESPTOOLPY_FLASHFREQ": "40m",
        "CONFIG_ESP_BOOTLOADER_OFFSET": "0x0000", "CONFIG_ESP_BOOTLOADER_SIZE": hex(boot["bootloader_bytes"]),
        "CONFIG_ESP_IMAGE0_PRIMARY_START_ADDRESS": hex(layout["primary_offset"]),
        "CONFIG_ESP_APPLICATION_SIZE": hex(layout["slot_bytes"]),
        "CONFIG_ESP_IMAGE0_SECONDARY_START_ADDRESS": hex(layout["secondary_offset"]),
        "CONFIG_ESP_SCRATCH_OFFSET": hex(layout["scratch_offset"]),
        "CONFIG_ESP_SCRATCH_SIZE": hex(layout["scratch_bytes"]),
        "CONFIG_ESP_MCUBOOT_WDT_ENABLE": 1, "CONFIG_LIBC_NEWLIB": 1,
        "CONFIG_ESP_CONSOLE_UART": 1, "CONFIG_ESP_CONSOLE_UART_NUM": 0,
        "CONFIG_ESP_CONSOLE_ROM_SERIAL_PORT_NUM": 0, "CONFIG_BOOTLOADER_FLASH_XMC_SUPPORT": 1,
        "CONFIG_IDF_TARGET_ARCH_XTENSA": 1, "CONFIG_BOOTLOADER_LOG_LEVEL": 3,
        "CONFIG_ESP_USE_MBEDTLS": 1, "CONFIG_ESP_SIGN_RSA": 1, "CONFIG_ESP_SIGN_RSA_LEN": 3072,
        "CONFIG_ESP_SIGN_KEY_FILE": str(public_key), "CONFIG_ESP_DOWNGRADE_PREVENTION": 1,
        "CONFIG_ESP_DOWNGRADE_PREVENTION_SECURITY_COUNTER": 1}
    # SDK 'defined' checks treat '=0' as enabled. Unsupported security/flash/fuse
    # settings must be absent, never emitted as false numeric definitions.
    return "".join(key + "=" + str(value) + "\n" for key, value in values.items())


def verify_config(data, text):
    values = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            require(key not in values)
            values[key] = value
    config = data["configuration"]["kconfig"]
    for wanted in config["require"]:
        key, value = wanted.split("=", 1)
        require(values.get(key) == value)
    forbidden = config.get("forbid-regex", "")
    require(not forbidden or re.search(forbidden, text, re.MULTILINE) is None)
    for name in config["disable"]:
        require(values.get("CONFIG_" + name, "n") == "n")
    for key, value in native_settings(data["configuration"]["boot"]).items():
        require(int(values["CONFIG_" + key], 0) == value)
    return values


def record_payload(name, policy_path, pin, config_path, elf, hex_file, output):
    import sign
    from intelhex import IntelHex
    data, policy, _ = checked_policy(name, policy_path, pin)
    config = sign.untrusted_read(config_path, 2 * 1024 * 1024)
    verify_config(data, config.decode())
    layout = data["settings"]["layout"]
    encoded = sign.untrusted_read(hex_file, 4 * layout["slot_bytes"] + 65536)
    image = IntelHex(io.StringIO(encoded.decode("ascii")))
    require(image.minaddr() is not None and image.maxaddr() is not None
            and 0 < image.maxaddr() - image.minaddr() + 1 < layout["slot_bytes"])
    body = bytes(image.tobinarray())
    require(body[:4] not in (b"\x7fELF", b"\x3d\xb8\xf3\x96"))
    elf = Path(elf)
    require(elf.is_file() and not elf.is_symlink() and elf.stat().st_size <= 128 * 1024 * 1024)
    with elf.open("rb") as stream:
        elf_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    record = {"schema": 1, "scope": "unsigned-application-build-only", "platform": name,
        "policy_sha256": pin, "configuration_sha256": data["settings"]["configuration_sha256"],
        "resolved_config_sha256": digest(config), "elf_sha256": elf_digest,
        "intelhex_sha256": digest(encoded), "payload_sha256": digest(body), "payload_bytes": len(body),
        "intelhex_base_address": image.minaddr(), "profile_sources": data["profile_sources"],
        "deployment_permitted": False, "physical_qualified": False}
    output = Path(output)
    with sign.directory(output) as fd:
        sign.write_new(fd, "payload.bin", body)
        sign.write_new(fd, "payload-build.json", canonical(record) + b"\n")
        sign.write_new(fd, "boot-policy.json", policy)
        os.fsync(fd)
    return record
