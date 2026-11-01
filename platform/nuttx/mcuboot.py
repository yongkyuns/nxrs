"""NuttX product/build wiring for the upstream Espressif MCUboot port."""
from mcuboot_image import image_class, layout
from uuid import UUID

MCUBOOT_REVISION = "8a07053d42e592c85cb35b79c4de1b7749943387"
HAL_REVISION = "911dbec8e4a92e70056b58a3d2b0d965b8b7bcc9"


def protected(platform: dict) -> bool:
    value = platform.get("hardware-protection", False)
    if type(value) is not bool:
        raise ValueError("hardware-protection must be an explicit boolean")
    return value


def application_config(platform: dict) -> dict[str, str]:
    boot = layout(platform["mcuboot"])
    image_class(platform)
    if platform["target"] != "xtensa-esp32s3-nuttx":
        raise ValueError("MCUboot build wiring is not qualified for this NuttX target")
    if boot["flash-size"] != 0x1000000 or boot["primary-offset"] < 0x20000:
        raise ValueError("ESP32-S3 camera requires 16 MiB flash and reserved boot/eFuse regions")
    if boot["erase-size"] != 4096 or boot["slot-size"] // boot["erase-size"] != 512:
        raise ValueError("Pinned Espressif MCUboot requires 512 sectors per camera slot")
    if boot["write-align"] != (32 if protected(platform) else 4):
        raise ValueError("ESP32-S3 image alignment must match hardware flash protection")
    values = {
        "ESP32S3_OTA_PRIMARY_SLOT_OFFSET": hex(boot["primary-offset"]),
        "ESP32S3_OTA_SECONDARY_SLOT_OFFSET": hex(boot["secondary-offset"]),
        "ESP32S3_OTA_SLOT_SIZE": hex(boot["slot-size"]),
        "ESP32S3_OTA_SCRATCH_OFFSET": hex(boot["scratch-offset"]),
        "ESP32S3_OTA_SCRATCH_SIZE": hex(boot["scratch-size"]),
        "ESP32S3_APP_MCUBOOT_HEADER_SIZE": str(boot["header-size"]),
        "ESP32S3_MCUBOOT_VERSION": f'"{MCUBOOT_REVISION}"',
    }
    if protected(platform) and platform["hal"].get("boot") != "none":
        raise ValueError("encrypted MCUboot update control requires interrupted-write qualification")
    if platform["hal"].get("boot") == "nuttx" or protected(platform):
        values.update({
            "ESP32S3_SPIFLASH": "y",
            "ESP32S3_SPIFLASH_AUTO_BRINGUP": "n",
            "ESP32S3_STORAGE_MTD_OFFSET": hex(boot["scratch-offset"] + boot["scratch-size"]),
        })
    if platform["hal"].get("boot") == "nuttx":
        values.update({"BOOT_MCUBOOT": "y", "MCUBOOT_VERSION": f'"{MCUBOOT_REVISION}"'})
    if platform["hal"].get("config") == "nuttx":
        if platform["hal"].get("boot") != "nuttx" or protected(platform):
            raise ValueError("camera config storage requires the unencrypted MCUboot layout")
        # Two erase blocks, separate from firmware slots and swap scratch.
        values.update({"MTD_CONFIG_NVS": "y", "MTD_CONFIG_NONE": "n",
                       "MTD_CONFIG_NAMED": "y", "MTD_CONFIG_NAME_LEN": "32",
                       "ESP32S3_STORAGE_MTD_SIZE": "0x2000"})
    if platform["hal"].get("watchdog") == "nuttx":
        if platform["hal"].get("boot") != "nuttx":
            raise ValueError("inherited boot watchdog requires boot control")
        values.update({"WATCHDOG": "y", "ESP32S3_RWDT": "y", "ESP32S3_RWDT_INHERIT": "y"})
    if protected(platform):
        values["ESP32S3_OTA_PARTITION_ENCRYPT"] = "y"
        values["ESP32S3_SPIFLASH_MTD_BLKSIZE"] = "32"
    return values


def watchdog_timeout_ms(platform: dict) -> int:
    """A fixed bootloader-owned startup budget, capped at five minutes."""
    value = platform.get("boot-watchdog-ms", 9000)
    if type(value) is not int or not 1 <= value <= 300000:
        raise ValueError("boot-watchdog-ms must be an integer from 1 through 300000")
    return value


def bootloader_config(platform: dict, root: str) -> str:
    """Only the public application root enters the SDK bootloader build."""
    application_config(platform)
    if any(char in root for char in ('\n', '\r', ';', '"', '\\')) or not root.startswith('/'):
        raise ValueError("public root needs an absolute path without CMake delimiters")
    boot = platform["mcuboot"]
    values = {
        "NON_OS_BUILD": 1, "CONFIG_ESPRESSIF_BOOTLOADER_MCUBOOT": 1,
        "CONFIG_ESP_BOOTLOADER_OFFSET": "0x0", "CONFIG_ESP_BOOTLOADER_SIZE": "0xf000",
        "CONFIG_ESP_IMAGE0_PRIMARY_START_ADDRESS": hex(boot["primary-offset"]),
        "CONFIG_ESP_IMAGE0_SECONDARY_START_ADDRESS": hex(boot["secondary-offset"]),
        "CONFIG_ESP_APPLICATION_SIZE": hex(boot["slot-size"]),
        "CONFIG_ESP_SCRATCH_OFFSET": hex(boot["scratch-offset"]),
        "CONFIG_ESP_SCRATCH_SIZE": hex(boot["scratch-size"]),
        "CONFIG_LIBC_NEWLIB": 1, "CONFIG_IDF_TARGET_ARCH_XTENSA": 1,
        "CONFIG_BOOTLOADER_FLASH_XMC_SUPPORT": 1,
        "CONFIG_BOOTLOADER_LOG_LEVEL": 3,
        "CONFIG_BOOTLOADER_WDT_ENABLE": 1,
        "CONFIG_BOOTLOADER_WDT_TIME_MS": watchdog_timeout_ms(platform),
        "CONFIG_ESP_MCUBOOT_WDT_ENABLE": 1,
        "CONFIG_ESP_CONSOLE_UART": 1, "CONFIG_ESP_CONSOLE_UART_NUM": 0,
        "CONFIG_ESP_CONSOLE_ROM_SERIAL_PORT_NUM": 0,
        "CONFIG_ESP_SIGN_EC256": 1, "CONFIG_ESP_USE_TINYCRYPT": 1,
        "CONFIG_ESP_DOWNGRADE_PREVENTION": 1,
        "CONFIG_ESP_DOWNGRADE_PREVENTION_SECURITY_COUNTER": 1,
        "CONFIG_ESP_SIGN_KEY_FILE": root,
        "MCUBOOT_UUID_CID": 1,
        "CONFIG_ESP_IMAGE_CLASS_ID": ",".join(f"0x{byte:02x}" for byte in UUID(image_class(platform)).bytes),
    }
    if protected(platform):
        values.update({key: 1 for key in (
            "CONFIG_SECURE_SIGNED_ON_BOOT", "CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME",
            "CONFIG_SECURE_BOOT", "CONFIG_SECURE_BOOT_V2_ENABLED",
            "CONFIG_SECURE_BOOT_SUPPORTS_RSA", "CONFIG_SECURE_FLASH_ENC_ENABLED",
            "CONFIG_SECURE_FLASH_ENCRYPTION_MODE_RELEASE",
            "CONFIG_SECURE_FLASH_REQUIRE_ALREADY_ENABLED",
            "CONFIG_SECURE_BOOT_REQUIRE_ALREADY_ENABLED", "CONFIG_EFUSE_WRITES_FORBIDDEN",
        )})
    return "".join(f"{key}={value}\n" for key, value in values.items())
