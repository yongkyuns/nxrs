#!/usr/bin/env python3
"""Build signature-enforcing MCUboot from pinned local Git objects. Never flash."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform/nuttx"))
from mcuboot import application_config, bootloader_config, protected, watchdog_timeout_ms, MCUBOOT_REVISION, HAL_REVISION
from platform_config import resolve
from mcuboot_image import image_class


def export(repository: Path, revision: str, destination: Path) -> None:
    destination.mkdir()
    # Git archives the pin, ignoring checkout changes and leaving the cache untouched.
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(repository), "archive", revision],
                       stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as source:
            source.extractall(destination, filter="data")


def sign_rom(source: Path, output: Path, key: Path, root: Path) -> str:
    """Use Espressif's RSA-3072 signer and independently supplied public root."""
    from importlib.metadata import version
    if version("esptool") != "4.8.1":
        raise ValueError("ROM signing requires esptool==4.8.1")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    public = root.read_bytes()
    parsed = serialization.load_pem_public_key(public)
    if not isinstance(parsed, rsa.RSAPublicKey) or parsed.key_size != 3072:
        raise ValueError("ESP32-S3 ROM root must be an RSA-3072 PUBLIC key")
    if output.resolve() in {path.resolve() for path in (source, key, root)}:
        raise ValueError("signed bootloader must not replace an input or key")
    with tempfile.TemporaryDirectory(prefix=".rom-sign-", dir=output.parent) as directory:
        temporary = Path(directory)
        expected = temporary / "root.pem"
        expected.write_bytes(public)
        candidate = temporary / "bootloader.bin"
        subprocess.run([sys.executable, "-m", "espsecure", "sign_data", "--version", "2",
                        "--keyfile", str(key), "--output", str(candidate), str(source)], check=True)
        subprocess.run([sys.executable, "-m", "espsecure", "verify_signature", "--version", "2",
                        "--keyfile", str(expected), str(candidate)], check=True)
        if not 0 < candidate.stat().st_size <= 0xf000:
            raise ValueError("ROM signature exceeds the reserved bootloader region")
        os.replace(candidate, output)
    return hashlib.sha256(public).hexdigest()


def build(platform: dict, mcuboot: Path, hal: Path, root: Path, out: Path,
          rom_key: Path | None = None, rom_root: Path | None = None) -> None:
    application_config(platform)
    if protected(platform) and (rom_key is None or rom_root is None):
        raise ValueError("protected builds require both --rom-key and --rom-root")
    if not protected(platform) and (rom_key is not None or rom_root is not None):
        raise ValueError("ROM signing inputs require the protected product profile")
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
    patches = []
    if protected(platform):
        for component, folder, name in (
                ("mcuboot", "mcuboot", "0002-require-provisioned-protection.patch"),
                ("mcuboot", "mcuboot", "0003-encrypted-write-once-flash.patch"),
                ("esp-hal-3rdparty", "hal", "0004-forbid-efuse-programming.patch")):
            patch = ROOT / "upstream" / component / "patches" / name
            subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=out / folder, check=True)
            patches.append({"component": component, "patch": name,
                            "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()})
    for name in ("0006-espressif-image-class.patch", "0007-espressif-watchdog-budget.patch"):
        patch = ROOT / "upstream/mcuboot/patches" / name
        subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=out / "mcuboot", check=True)
        patches.append({"component": "mcuboot", "patch": patch.name,
                        "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()})
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
    rom = {}
    if protected(platform):
        cache = (out / "build/CMakeCache.txt").read_text()
        nm = next(line.split("=", 1)[1] for line in cache.splitlines() if line.startswith("CMAKE_NM:FILEPATH="))
        symbols = subprocess.check_output([nm, "--defined-only", str(out / "build/mcuboot_esp32s3.elf")], text=True)
        if any(line.split()[-1:] == ["efuse_hal_program"] for line in symbols.splitlines()):
            raise ValueError("protected bootloader still contains the physical eFuse programmer")
        signed = out / "bootloader.signed.bin"
        fingerprint = sign_rom(binary, signed, rom_key, rom_root)
        binary = signed
        rom = {"rom_signature": "rsa-3072-pss-sha256", "rom_root_pem_sha256": fingerprint,
               "hardware_qualified": False, "requires_factory_provisioning": True,
               "efuse_writes_forbidden": True, "encrypted_write_once": True}
    (out / "bootloader.json").write_text(json.dumps({
        "mcuboot": MCUBOOT_REVISION, "esp-hal-3rdparty": HAL_REVISION,
        "platform": platform["name"], "layout": platform["mcuboot"],
        "image_class": image_class(platform),
        "boot_watchdog_ms": watchdog_timeout_ms(platform),
        "application_signature": "ecdsa-p256-sha256",
        "public_root_sha256": hashlib.sha256(public_root.read_bytes()).hexdigest(),
        "bootloader_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "bootloader_file": str(binary.relative_to(out)),
        "hardware_secure_boot": protected(platform), "flash_encryption": protected(platform),
        "patches": patches,
        **rom,
    }, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--mcuboot-repo", type=Path, required=True)
    parser.add_argument("--hal-repo", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rom-key", type=Path, help="operator-owned ESP32-S3 RSA-3072 signing key")
    parser.add_argument("--rom-root", type=Path, help="independent expected RSA-3072 PUBLIC key")
    args = parser.parse_args()
    try:
        build(resolve(args.platform), args.mcuboot_repo.resolve(), args.hal_repo.resolve(),
              args.root.resolve(), args.out.resolve(),
              args.rom_key.resolve() if args.rom_key else None,
              args.rom_root.resolve() if args.rom_root else None)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"MCUboot build failed: {error}\n")


if __name__ == "__main__":
    main()
