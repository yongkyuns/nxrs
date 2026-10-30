#!/usr/bin/env python3
"""Build signature-enforcing MCUboot from pinned local Git objects. Never flash."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform/nuttx"))
from mcuboot import application_config, bootloader_config, MCUBOOT_REVISION, HAL_REVISION
from platform_config import resolve


def export(repository: Path, revision: str, destination: Path) -> None:
    destination.mkdir()
    # Git archives the pin, ignoring checkout changes and leaving the cache untouched.
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(repository), "archive", revision],
                       stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as source:
            source.extractall(destination, filter="data")


def build(platform: dict, mcuboot: Path, hal: Path, root: Path, out: Path) -> None:
    application_config(platform)
    # Reject private/unsupported roots without ever copying a private key into the build.
    public = subprocess.check_output([sys.executable, "-c",
                    "from imgtool.keys import load, ECDSA256P1Public; "
                    "import sys; k=load(sys.argv[1]); "
                    "type(k) is ECDSA256P1Public or sys.exit('expected EC P-256 PUBLIC key'); "
                    "sys.stdout.buffer.write(k.get_public_pem())", str(root)])
    bootloader_config(platform, str(out / "root.pem"))
    out.mkdir(parents=True, exist_ok=False)
    export(mcuboot, MCUBOOT_REVISION, out / "mcuboot")
    export(hal, HAL_REVISION, out / "hal")
    public_root = out / "root.pem"
    public_root.write_bytes(public)
    config = bootloader_config(platform, str(public_root))
    (out / "bootloader.conf").write_text(config)
    subprocess.run(["cmake", "-S", str(out / "mcuboot/boot/espressif"),
                    "-B", str(out / "build"), "-G", "Ninja",
                    "-DMCUBOOT_TARGET=esp32s3", f"-DMCUBOOT_CONFIG_FILE={out / 'bootloader.conf'}",
                    f"-DESP_HAL_PATH={out / 'hal'}", "-DCONFIG_ESP_FLASH_SIZE=16MB",
                    "-DESP_FLASH_MODE=dio", "-DESP_FLASH_FREQ=40m",
                    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"], check=True)
    subprocess.run(["cmake", "--build", str(out / "build"), "--parallel", "2"], check=True)
    binary = out / "build/mcuboot_esp32s3.bin"
    if not 0 < binary.stat().st_size <= 0xf000:
        raise ValueError("bootloader exceeds the reserved flash region")
    (out / "bootloader.json").write_text(json.dumps({
        "mcuboot": MCUBOOT_REVISION, "esp-hal-3rdparty": HAL_REVISION,
        "platform": platform["name"], "layout": platform["mcuboot"],
        "application_signature": "ecdsa-p256-sha256",
        "public_root_sha256": hashlib.sha256(public_root.read_bytes()).hexdigest(),
        "bootloader_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "hardware_secure_boot": False, "flash_encryption": False,
    }, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--mcuboot-repo", type=Path, required=True)
    parser.add_argument("--hal-repo", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(resolve(args.platform), args.mcuboot_repo.resolve(), args.hal_repo.resolve(),
              args.root.resolve(), args.out.resolve())
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"MCUboot build failed: {error}\n")


if __name__ == "__main__":
    main()
