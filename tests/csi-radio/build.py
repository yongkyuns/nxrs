#!/usr/bin/env python3
"""Build-only CSI qualification images. Never opens a device or flashes firmware."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PINS = json.loads((HERE / "pins.json").read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stage_package(name, package, destination):
    """Extract only named SDK inputs after checking the complete package digest."""
    pin = PINS["packages"][name]
    if sha(package) != pin["zip_sha256"]:
        raise ValueError(f"{name}: package SHA256 mismatch")
    names = [f"6.0/esp32s3/lib{name}.a", f"include/{name}.h", "LICENSE"]
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        for relative in names:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(f"{name}/{relative}"))
    library = destination / names[0]
    if sha(library) != pin["archive_sha256"]:
        raise ValueError(f"{name}: archive SHA256 mismatch")
    return library


def run(command, **kwargs):
    subprocess.run([str(x) for x in command], check=True, **kwargs)


def validate_output(path):
    requested = Path(path).absolute()
    if requested.is_symlink():
        raise ValueError("output must not be a symlink")
    path = requested.resolve()
    # The common builder replaces its output. Keep that operation confined to
    # this fixture's explicitly named build namespace, never another deployment.
    if path.parent != (ROOT / "target").resolve() or not path.name.startswith(
        "csi-radio-"
    ):
        raise ValueError("out must be target/csi-radio-NAME in this checkout")
    if path.is_symlink():
        raise ValueError("output must not be a symlink")
    return path


def source_pin(component, expected):
    source = ROOT / "external" / component
    actual = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", f":external/{component}"], text=True
    ).strip()
    if actual != expected or not (source / ".git").exists():
        raise ValueError(f"initialize the pinned external/{component} submodule")
    return source


def kernel(args, out, vendor, environment):
    out.mkdir(parents=True, exist_ok=True)
    nuttx, apps = out / "nuttx", out / "apps"
    # Refuse to delete/reconfigure a previous image implicitly. Choose a fresh
    # namespace or explicitly remove this fixture's old output yourself.
    if nuttx.exists() or apps.exists():
        raise ValueError(
            "kernel output already configured; choose a fresh csi-radio-NAME"
        )
    for component, destination, expected in [
        ("nuttx", nuttx, PINS["nuttx"]),
        ("nuttx-apps", apps, PINS["nuttx_apps"]),
    ]:
        source = source_pin(component, expected)
        destination.mkdir()
        with subprocess.Popen(
            ["git", "-C", str(source), "archive", expected], stdout=subprocess.PIPE
        ) as producer:
            run(["tar", "-x", "-C", destination], stdin=producer.stdout)
            if producer.wait() != 0:
                raise RuntimeError("dependency archive failed")
        run(
            [
                "python3",
                ROOT / "tools/apply-nuttx-patches.py",
                "--component",
                component,
                "--source",
                destination,
                "--revision",
                expected,
                "--record",
                out / f"{component}-patches.json",
            ]
        )
    run(
        [
            "python3",
            ROOT / "tools/nuttx-version.py",
            "--source",
            nuttx,
            "--revision",
            PINS["nuttx"],
        ]
    )
    sdk = nuttx / "arch/xtensa/src/esp32s3/esp-hal-3rdparty"
    if args.sdk_cache:
        run(
            [
                "bash",
                ROOT / "tools/prepare-esp-hal-cache.sh",
                args.sdk_cache,
                sdk,
                PINS["esp_hal"],
            ]
        )
    app = apps / "examples/csikernel"
    app.mkdir()
    (app / "Kconfig").write_text(
        'config EXAMPLES_CSIKERNEL\n\tbool "CSI kernel SDK witness"\n\tdefault n\n'
    )
    (app / "Make.defs").write_text(
        "ifneq ($(CONFIG_EXAMPLES_CSIKERNEL),)\nCONFIGURED_APPS += $(APPDIR)/examples/csikernel\nendif\n"
    )
    (app / "Makefile").write_text(
        "include $(APPDIR)/Make.defs\nPROGNAME = csikernel\nPRIORITY = 100\nSTACKSIZE = 16384\nMODULE = $(CONFIG_EXAMPLES_CSIKERNEL)\nMAINSRC = kernel.c\nCFLAGS += $(NXRS_TARGET_C_FLAGS)\ninclude $(APPDIR)/Application.mk\n"
    )
    shutil.copy2(HERE / "kernel.c", app / "kernel.c")
    run(
        [
            "bash",
            "tools/configure.sh",
            "-m" if os.uname().sysname == "Darwin" else "-l",
            "esp32s3-devkit:nsh",
        ],
        cwd=nuttx,
        env=environment,
    )
    for symbol in [
        "ESP32S3_QEMU_IMAGE",
        "EXAMPLES_CSIKERNEL",
        "DEBUG_FEATURES",
        "DEBUG_ASSERTIONS",
        "DEBUG_SYMBOLS",
        "DEBUG_LINK_MAP",
        "STACK_COLORATION",
        "NSH_ARGCAT",
        "LIBC_FLOATINGPOINT",
        "PTHREAD_MUTEX_TYPES",
        "DEV_URANDOM",
    ]:
        run(
            ["kconfig-tweak", "--enable", "CONFIG_" + symbol],
            cwd=nuttx,
            env=environment,
        )
    for symbol in [
        "NET",
        "SMP",
        "ESPRESSIF_WIFI",
        "ESPRESSIF_BLE",
        "ESP32S3_SPIRAM",
        "ARCH_FPU",
        "SIG_DEFAULT",
        "NSH_DISABLEBG",
    ]:
        run(
            ["kconfig-tweak", "--disable", "CONFIG_" + symbol],
            cwd=nuttx,
            env=environment,
        )
    run(
        [
            "kconfig-tweak",
            "--set-val",
            "CONFIG_ARCH_INTERRUPTSTACK",
            "4096",
            "--set-val",
            "CONFIG_PTHREAD_STACK_DEFAULT",
            "16384",
        ],
        cwd=nuttx,
        env=environment,
    )
    run(["make", "olddefconfig"], cwd=nuttx, env=environment)
    run(["make", "context"], cwd=nuttx, env=environment)
    run(
        [
            "python3",
            ROOT / "tools/apply-nuttx-patches.py",
            "--component",
            "esp-hal-3rdparty",
            "--source",
            sdk,
            "--revision",
            PINS["esp_hal"],
            "--build-checkout",
            "--record",
            out / "esp-hal-patches.json",
        ]
    )
    environment.update(link_environment(out, vendor, sdk, "kernel"))
    run(["make", f"-j{args.jobs}"], cwd=nuttx, env=environment)


def link_environment(out, vendor, sdk, mode, csi_init=True):
    includes = [vendor / name / "include" for name in PINS["packages"]]
    includes += [
        sdk.parents[4] / "sched",
        sdk / "nuttx/esp32s3/include",
        sdk / "nuttx/include",
        sdk / "components/soc/esp32s3/include",
        sdk / "components/xtensa/include",
        sdk / "components/xtensa/esp32s3/include",
    ]
    includes += [
        sdk / "components" / name / "include"
        for name in [
            "esp_common",
            "esp_wifi",
            "esp_event",
            "esp_netif",
            "esp_hw_support",
            "esp_rom",
            "esp_phy",
            "heap",
            "esp_timer",
            "esp_system",
        ]
    ]
    includes += [
        sdk / "components/esp_wifi/include/local",
        sdk / "components/esp_phy/esp32s3/include",
    ]
    return {
        "NXRS_TARGET_C_FLAGS": " ".join("-isystem" + str(p) for p in includes)
        + f" -DNXRS_CSI_TEST_INIT={int(csi_init)}",
        "EXTRA_LIBS": " ".join(
            str(vendor / name / f"6.0/esp32s3/lib{name}.a") for name in PINS["packages"]
        ),
        "EXTRALINKCMDS": (
            "--wrap=esp_wifi_init"
            if mode == "radio"
            else "--wrap=malloc --wrap=free --wrap=phy_force_rx_gain --wrap=phy_fft_scale_force"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["kernel", "radio"])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--gain-package", required=True, type=Path)
    parser.add_argument("--motion-package", required=True, type=Path)
    parser.add_argument("--sdk-cache", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument(
        "--no-csi-init",
        action="store_true",
        help="radio control image: preserve the SDK's normal initialization config",
    )
    args = parser.parse_args()
    if not 1 <= args.jobs <= 32:
        parser.error("jobs must be 1..32")
    if args.no_csi_init and args.mode != "radio":
        parser.error("--no-csi-init applies only to the radio control image")
    out = validate_output(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source_pin("nuttx", PINS["nuttx"])
    source_pin("nuttx-apps", PINS["nuttx_apps"])
    # Vendor staging is a sibling because the ordinary builder clears out/*.json
    # and its own build trees. Staging never contains credentials or radio data.
    vendor = ROOT / "target" / f"{out.name}-vendor"
    libraries = [
        stage_package(name, package, vendor / name)
        for name, package in zip(
            PINS["packages"], [args.gain_package, args.motion_package]
        )
    ]
    environment = os.environ.copy()
    environment["NXRS_NUTTX_BUILD_JOBS"] = str(args.jobs)
    environment["CROSSDEV"] = "xtensa-esp32s3-elf-"
    if args.mode == "kernel":
        kernel(args, out, vendor, environment)
    else:
        sdk = out / "nuttx/arch/xtensa/src/chip/esp-hal-3rdparty"
        environment.update(
            link_environment(out, vendor, sdk, "radio", not args.no_csi_init)
        )
        if args.sdk_cache:
            environment["NXRS_ESP_HAL_3RDPARTY_CACHE"] = str(args.sdk_cache.resolve())
            environment["NXRS_ESP_HAL_3RDPARTY_REVISION"] = PINS["esp_hal"]
        run(
            [
                "bash",
                ROOT / "tools/build-nuttx-std-app.sh",
                "--app-manifest",
                HERE / "Cargo.toml",
                "--app-package",
                "nxrs-csi-radio-test",
                "--bin",
                "nxrs-csi-radio-test",
                "--command",
                "csi_radio",
                "--priority",
                "100",
                "--stack-size",
                "16384",
                "--platform",
                "esp32s3-wifi",
                "--kconfig-set",
                "LINE_MAX=256",
                "--kconfig-set",
                "NSH_MAXARGUMENTS=12",
                "--out",
                out,
                "--target-c-source",
                ROOT / "tests/nuttx-std/wifi/ffi/credentials.c",
                "--target-c-source",
                HERE / "ffi/csi.c",
                "--size-optimized",
                "--trace-only-backtrace",
            ],
            env=environment,
            cwd=ROOT,
        )
    record_build(args.mode, out, libraries, not args.no_csi_init)


def check_console_config(text):
    required = {"CONFIG_LINE_MAX=256", "CONFIG_NSH_MAXARGUMENTS=12"}
    if not required <= set(text.splitlines()):
        raise ValueError("CSI test console cannot carry the full command")
    return {"line_max": 256, "max_arguments": 12}


def record_build(mode, out, libraries, csi_init=True):
    symbols = subprocess.check_output(
        ["xtensa-esp32s3-elf-nm", "-g", str(out / "nuttx/nuttx")], text=True
    )
    if any(line.split() and line.split()[0] == "U" for line in symbols.splitlines()):
        raise RuntimeError("final image has undefined imports")
    required = [
        "esp_csi_gain_ctrl_get_gain_compensation",
        "esp_csi_gain_ctrl_get_rx_gain",
    ]
    required += (
        [
            "__wrap_esp_wifi_init",
            "nxrs_csi_test_translate",
            "esp_wifi_set_csi",
            "esp_wifi_set_csi_rx_cb",
            "esp_wifi_set_csi_config",
        ]
        if mode == "radio"
        else [
            "esp_radar_motion_dec_compute",
            "esp_radar_motion_dec_compute_jitter",
            "esp_radar_motion_dec_wander_compute",
        ]
    )
    defined = {
        line.split()[-1]
        for line in symbols.splitlines()
        if len(line.split()) == 3 and line.split()[1].upper() in {"T", "W"}
    }
    if not set(required) <= defined:
        raise RuntimeError("final image discarded required witness symbols")
    fixture_sources = (
        [HERE / "kernel.c"]
        if mode == "kernel"
        else [
            HERE / "src/main.rs",
            HERE / "src/lib.rs",
            HERE / "src/lifecycle.rs",
            HERE / "src/echo.rs",
            HERE / "src/handoff.rs",
            HERE / "Cargo.toml",
            ROOT / "Cargo.lock",
            HERE / "ffi/csi.c",
            HERE / "ffi/csi.h",
            ROOT / "tests/nuttx-std/wifi/ffi/credentials.c",
        ]
    )
    report = {
        "mode": mode,
        "csi_init_enabled": csi_init if mode == "radio" else None,
        "compiler": subprocess.check_output(
            ["xtensa-esp32s3-elf-gcc", "--version"], text=True
        ).splitlines()[0],
        "pins": PINS,
        "hardware_qualified": False,
        "device_access": False,
        "required_symbols": required,
        "source_sha256": {str(p.relative_to(ROOT)): sha(p) for p in fixture_sources},
        "libraries": {str(p.relative_to(ROOT)): sha(p) for p in libraries},
        "image_sha256": sha(out / "nuttx/nuttx.merged.bin"),
        "fixture_sha256": sha(
            HERE / "kernel.c" if mode == "kernel" else HERE / "src/main.rs"
        ),
    }
    if mode == "radio":
        report["console"] = check_console_config((out / "nuttx/.config").read_text())
    (out / "csi-build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"BUILT ONLY: {out}/nuttx/nuttx.merged.bin; no flash/device access; hardware qualification pending"
    )


if __name__ == "__main__":
    main()
