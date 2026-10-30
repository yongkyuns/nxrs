#!/usr/bin/env python3
"""Package firmware with upstream imgtool; no device access or key generation."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def layout(config: dict) -> dict:
    """Geometry shared by the product build and image packager, in bytes."""
    fields = {"flash-size", "primary-offset", "secondary-offset", "slot-size",
              "scratch-offset", "scratch-size", "header-size", "write-align", "erase-size"}
    if not isinstance(config, dict) or set(config) != fields:
        raise ValueError("mcuboot requires explicit flash, slot, scratch, header and alignment geometry")
    if any(type(value) is not int or not (0 if key.endswith("-offset") else 1) <= value <= 0xffffffff
           for key, value in config.items()):
        raise ValueError("mcuboot geometry needs unsigned offsets and positive 32-bit byte counts")
    if config["write-align"] not in {1, 2, 4, 8, 16, 32}:
        raise ValueError("mcuboot write-align must be a power of two from 1 through 32")
    if config["header-size"] < 32 or config["header-size"] % config["write-align"]:
        raise ValueError("mcuboot header must be at least 32 bytes and write-aligned")
    sector = config["erase-size"]
    if sector & (sector - 1) or sector % config["write-align"]:
        raise ValueError("mcuboot erase-size must be a power of two and write-aligned")
    end = 0
    for offset, size in sorted(((config["primary-offset"], config["slot-size"]),
                                (config["secondary-offset"], config["slot-size"]),
                                (config["scratch-offset"], config["scratch-size"]))):
        if offset % sector or size % sector or offset < end:
            raise ValueError("mcuboot regions must be disjoint and flash-sector aligned")
        end = offset + size
    if end > config["flash-size"] or config["slot-size"] <= config["header-size"]:
        raise ValueError("mcuboot layout exceeds flash or leaves no image space")
    return config


def sign(source: Path, output: Path, config: dict, key: Path, root: Path,
         version: str, counter: int, *, confirm: bool = False) -> None:
    """Publish only after upstream verifies against the independently supplied root."""
    layout(config)
    if not 0 <= counter <= 0xffffffff:
        raise ValueError("security counter must be an explicit unsigned 32-bit integer")
    if output.resolve() in {path.resolve() for path in (source, key, root)}:
        raise ValueError("output must not replace an input or key")
    for path in (source, key, root):
        if not path.is_file():
            raise ValueError("firmware input, signing key and verification root must exist")
    # Constrain the tool version used by the pinned NuttX MCUboot integration.
    if subprocess.check_output(["imgtool", "version"], text=True).strip() != "2.2.0":
        raise ValueError("install tools/mcuboot-requirements.txt for the pinned MCUboot integration")
    with tempfile.TemporaryDirectory(prefix=".mcuboot-", dir=output.parent) as directory:
        candidate = Path(directory) / "image.bin"
        command = ["imgtool", "sign", "-k", str(key), "-v", version,
                   "-s", str(counter), "-H", str(config["header-size"]), "--pad-header",
                   "-S", str(config["slot-size"]), "--align", str(config["write-align"]),
                   "--max-align", str(max(8, config["write-align"])),
                   "--max-sectors", str(config["slot-size"] // config["erase-size"])]
        if confirm:
            command.extend(["--pad", "--confirm"])
        subprocess.run([*command, str(source), str(candidate)], check=True)
        subprocess.run(["imgtool", "verify", "-k", str(root), str(candidate)], check=True)
        os.replace(candidate, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--security-counter", type=int, required=True)
    parser.add_argument("--confirm", action="store_true", help="initial factory image only")
    args = parser.parse_args()
    from platform_config import resolve
    try:
        config = resolve(args.platform)["mcuboot"]
        sign(args.input, args.output, config, args.key, args.root,
             args.version, args.security_counter, confirm=args.confirm)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"MCUboot packaging failed: {error}\n")


if __name__ == "__main__":
    main()
