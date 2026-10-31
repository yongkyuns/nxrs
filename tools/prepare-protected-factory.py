#!/usr/bin/env python3
"""Prepare verified ESP32-S3 factory ciphertext files. Never flash or provision."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform/nuttx"))
from mcuboot import application_config, protected, MCUBOOT_REVISION, HAL_REVISION
from platform_config import resolve


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(profile: dict, boot_build: Path, application: Path, rom_root: Path,
            app_root: Path, flash_key: Path, output: Path) -> None:
    application_config(profile)
    if not protected(profile) or output.exists() or output.is_symlink():
        raise ValueError("factory preparation requires a protected profile and a fresh output directory")
    if version("esptool") != "4.8.1" or version("imgtool") != "2.2.0":
        raise ValueError("install esptool==4.8.1 and tools/mcuboot-requirements.txt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from imgtool.image import Image
    from imgtool.keys import load, ECDSA256P1Public

    public = rom_root.read_bytes()
    rom = serialization.load_pem_public_key(public)
    app = load(str(app_root))
    if not isinstance(rom, rsa.RSAPublicKey) or rom.key_size != 3072 or type(app) is not ECDSA256P1Public:
        raise ValueError("independent RSA-3072 and EC P-256 PUBLIC roots are required")
    key = flash_key.read_bytes()
    if len(key) != 32:
        raise ValueError("ESP32-S3 XTS_AES_128_KEY input must contain exactly 32 bytes")
    geometry = profile["mcuboot"]
    metadata = json.loads((boot_build / "bootloader.json").read_text())
    expected = {"mcuboot": MCUBOOT_REVISION, "esp-hal-3rdparty": HAL_REVISION,
                "platform": profile["name"], "layout": geometry,
                "bootloader_file": "bootloader.signed.bin",
                "hardware_secure_boot": True, "flash_encryption": True,
                "efuse_writes_forbidden": True,
                "encrypted_write_once": True,
                "rom_root_pem_sha256": digest(public),
                "public_root_sha256": digest(app.get_public_pem())}
    if any(metadata.get(field) != value for field, value in expected.items()):
        raise ValueError("boot build does not match the protected profile and expected roots")
    boot = (boot_build / "bootloader.signed.bin").read_bytes()
    image = application.read_bytes()
    if not 0 < len(boot) <= 0xf000 or len(boot) % 32 or digest(boot) != metadata["bootloader_sha256"]:
        raise ValueError("bootloader size, alignment or build digest mismatch")
    if len(image) != geometry["slot-size"]:
        raise ValueError("factory application must be a full --confirm slot image")
    # Derive the entire fresh confirmed trailer from the pinned upstream tool.
    trailer = Image(align=32, max_align=32, max_sectors=geometry["slot-size"] // geometry["erase-size"],
                    confirm=True)
    trailer.payload = bytearray()
    trailer.pad_to(trailer._trailer_size(32, trailer.max_sectors, False, None, False, 0))
    if image[-len(trailer.payload):] != trailer.payload:
        raise ValueError("factory application needs a fresh upstream confirmed trailer")

    with tempfile.TemporaryDirectory(prefix=".factory-", dir=output.parent) as directory:
        work = Path(directory)
        bundle = work / "bundle"
        bundle.mkdir()
        # Snapshot inputs for all SDK operations; private temporary files never enter the bundle.
        for name, data in (("rom.pem", public), ("app.pem", app.get_public_pem()), ("key.bin", key),
                           ("boot.bin", boot), ("primary.bin", image)):
            path = work / name
            path.write_bytes(data)
            path.chmod(0o600)
        subprocess.run([sys.executable, "-m", "espsecure", "verify_signature", "--version", "2",
                        "--keyfile", str(work / "rom.pem"), str(work / "boot.bin")], check=True)
        subprocess.run(["imgtool", "verify", "-k", str(work / "app.pem"), str(work / "primary.bin")], check=True)
        regions = []
        for name, offset, plaintext in (
                ("boot", 0, boot), ("primary", geometry["primary-offset"], image),
                ("secondary", geometry["secondary-offset"], b"\xff" * geometry["slot-size"]),
                ("scratch", geometry["scratch-offset"], b"\xff" * geometry["scratch-size"])):
            source = work / f"{name}.bin"
            source.write_bytes(plaintext)
            encrypted = bundle / f"{name}.enc.bin"
            decrypted = work / "decrypted.bin"
            for operation, src, dst in (("encrypt_flash_data", source, encrypted),
                                        ("decrypt_flash_data", encrypted, decrypted)):
                if operation == "decrypt_flash_data":
                    # Executable/padded image bytes stay encrypted for direct XIP.
                    # Only unwritten trailer/empty-slot AES blocks remain erased.
                    start = len(plaintext) if name == "boot" else (
                        len(image) - len(trailer.payload) if name == "primary" else 0)
                    ciphertext = bytearray(encrypted.read_bytes())
                    for position in range(start, len(plaintext), 16):
                        if plaintext[position:position + 16] == b"\xff" * 16:
                            ciphertext[position:position + 16] = b"\xff" * 16
                    encrypted.write_bytes(ciphertext)
                subprocess.run([sys.executable, "-m", "espsecure", operation, "--aes_xts",
                                "--keyfile", str(work / "key.bin"), "--address", hex(offset),
                                "--output", str(dst), str(src)], check=True)
            decoded = bytearray(decrypted.read_bytes())
            for position in range(0, len(decoded), 16):
                if ciphertext[position:position + 16] == b"\xff" * 16:
                    if position < start:
                        raise ValueError("executable ciphertext collides with the erased AES-block marker")
                    decoded[position:position + 16] = b"\xff" * 16
            if decoded != plaintext:
                raise ValueError("factory ciphertext failed address-specific SDK round-trip verification")
            regions.append({"file": encrypted.name, "offset": offset, "size": encrypted.stat().st_size,
                            "sha256": digest(encrypted.read_bytes())})
        (bundle / "factory.json").write_text(json.dumps({
            "platform": profile["name"], "layout": geometry, "regions": regions,
            "rom_root_pem_sha256": digest(public), "app_root_pem_sha256": digest(app.get_public_pem()),
            "flash_encryption": "xts-aes-128",
            "efuse_writes_forbidden": True,
            "encrypted_write_once": True,
            "hardware_qualified": False, "runtime_updates": False,
            "requires_factory_provisioning": True,
        }, indent=2) + "\n")
        os.rename(bundle, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    for name in ("boot-build", "application", "rom-root", "app-root", "flash-key", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(resolve(args.platform), args.boot_build, args.application, args.rom_root,
                args.app_root, args.flash_key, args.output)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Factory preparation failed: {error}\n")


if __name__ == "__main__":
    main()
