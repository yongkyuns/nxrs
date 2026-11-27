//! Generated from layouts/catalog.json; verify with refresh_bsp.py --check.
//! Bundled BSP data is read offline and is never executed.
use super::BuiltinLayout;

pub const SUPPORTED_LAYOUT_BOARDS: &[(&str, &str)] = &[
    ("mps2-an521-qemu", "MPS2-AN521 QEMU"),
    ("esp32s3-cores3", "ESP32-S3 CoreS3"),
    ("esp32s3-devkit", "ESP32-S3 DevKit / WROOM"),
    ("raspberrypi-pico-2", "Raspberry Pi Pico 2 (NSH)"),
    (
        "raspberrypi-pico-2-usbnsh",
        "Raspberry Pi Pico 2 / 2 W (USB NSH)",
    ),
];

pub(super) const BUILTIN_LAYOUTS: &[BuiltinLayout] = &[
    BuiltinLayout {
        id: "mps2-an521-qemu",
        label: "MPS2-AN521 QEMU",
        nuttx_board: "mps2-an521:nsh",
        nuttx_revision: "2b5c0de5e933281c209f954a63b99cf10c132346",
        required_config: &[
            ("CONFIG_ARCH_BOARD_MPS2_AN521", "y"),
            ("CONFIG_ARCH_CHIP_MPS2_AN521", "y"),
        ],
        json: include_str!("../layouts/mps2-an521-qemu.json"),
    },
    BuiltinLayout {
        id: "esp32s3-cores3",
        label: "ESP32-S3 CoreS3",
        nuttx_board: "esp32s3-m5-cores3:nsh",
        nuttx_revision: "2b5c0de5e933281c209f954a63b99cf10c132346",
        required_config: &[
            ("CONFIG_ARCH_CHIP_ESP32S3", "y"),
            ("CONFIG_ARCH_BOARD_ESP32S3_M5_CORES3", "y"),
        ],
        json: include_str!("../layouts/esp32s3.json"),
    },
    BuiltinLayout {
        id: "esp32s3-devkit",
        label: "ESP32-S3 DevKit / WROOM",
        nuttx_board: "esp32s3-devkit:nsh",
        nuttx_revision: "2b5c0de5e933281c209f954a63b99cf10c132346",
        required_config: &[
            ("CONFIG_ARCH_CHIP_ESP32S3", "y"),
            ("CONFIG_ARCH_BOARD_ESP32S3_DEVKIT", "y"),
        ],
        json: include_str!("../layouts/esp32s3.json"),
    },
    BuiltinLayout {
        id: "raspberrypi-pico-2",
        label: "Raspberry Pi Pico 2 (NSH)",
        nuttx_board: "raspberrypi-pico-2:nsh",
        nuttx_revision: "2b5c0de5e933281c209f954a63b99cf10c132346",
        required_config: &[
            ("CONFIG_ARCH_BOARD_RASPBERRYPI_PICO_2", "y"),
            ("CONFIG_ARCH_CHIP_RP23XX", "y"),
        ],
        json: include_str!("../layouts/raspberrypi-pico-2.json"),
    },
    BuiltinLayout {
        id: "raspberrypi-pico-2-usbnsh",
        label: "Raspberry Pi Pico 2 / 2 W (USB NSH)",
        nuttx_board: "raspberrypi-pico-2:usbnsh",
        nuttx_revision: "2b5c0de5e933281c209f954a63b99cf10c132346",
        required_config: &[
            ("CONFIG_ARCH_BOARD_RASPBERRYPI_PICO_2", "y"),
            ("CONFIG_ARCH_CHIP_RP23XX", "y"),
        ],
        json: include_str!("../layouts/raspberrypi-pico-2.json"),
    },
];
