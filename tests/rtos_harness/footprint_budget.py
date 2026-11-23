"""Check the linked flash, packaged image, and static RAM of ESP32-S3 firmware."""

import argparse
from pathlib import Path
import subprocess

from .images import esp_idf_section_accounting, parse_sections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("--linked-flash", type=int, required=True, help="maximum bytes")
    parser.add_argument("--image", type=int, required=True, help="maximum unpadded image bytes")
    parser.add_argument("--static-ram", type=int, required=True, help="maximum bytes")
    parser.add_argument("--readelf", default="xtensa-esp32s3-elf-readelf")
    args = parser.parse_args()

    nuttx = args.build / "nuttx"
    output = subprocess.check_output([args.readelf, "-W", "-h", "-S", nuttx / "nuttx"], text=True)
    if "Tensilica Xtensa" not in output or "EXEC (Executable file)" not in output:
        parser.error("expected a final Xtensa firmware ELF")
    totals = esp_idf_section_accounting(parse_sections(output))
    measurements = [
        ("Linked flash", totals["loadbearing_flash_bytes"], args.linked_flash),
        ("Packaged image", (nuttx / "nuttx.bin").stat().st_size, args.image),
        ("Static RAM", totals["resident_ram_bytes"], args.static_ram),
    ]
    for name, used, limit in measurements:
        if used <= 0 or limit <= 0:
            parser.error(f"{name} measurement and budget must be positive")
        status = "PASS" if used <= limit else "FAIL"
        print(f"{status} {args.build.name} {name}: {used:,} / {limit:,} B ({limit - used:+,} B remaining)")
    return int(any(used > limit for _, used, limit in measurements))


if __name__ == "__main__":
    raise SystemExit(main())
