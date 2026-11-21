#!/usr/bin/env python3
"""Independent post-link checks for the microphone and no-audio control images."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PINS = {
    "ESP32S3_I2C0_SDAPIN": "12", "ESP32S3_I2C0_SCLPIN": "11",
    "ESPRESSIF_I2S0_MCLKPIN": "0", "ESPRESSIF_I2S0_BCLKPIN": "34",
    "ESPRESSIF_I2S0_WSPIN": "33", "ESPRESSIF_I2S0_DINPIN": "14",
}
SYMBOLS = {"cores3_microphone_initialize", "es7210_initialize"}


def configuration(text):
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if not match and not disabled:
            continue
        name, value = (match[1], match[2]) if match else (disabled[1], "n")
        if name in values:
            raise ValueError(f"duplicate effective setting: {name}")
        values[name] = value
    return values


def verify(out, profile, nm_output):
    if profile not in ("cores3-microphone", "m5stack-cores3-gnss"):
        raise ValueError(f"unknown qualification profile: {profile}")
    nuttx = Path(out) / "nuttx"
    paths = [nuttx / name for name in (".config", "nuttx", "nuttx.bin", "nuttx.map")]
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing or empty target artifact: {path}")
    with (nuttx / "nuttx").open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError("nuttx is not an ELF file")
    config = configuration((nuttx / ".config").read_text())
    required = {"ARCH_BOARD_ESP32S3_M5_CORES3": "y", "ESP32S3_FLASH_16M": "y",
                "BUILD_FLAT": "y", "DEBUG_LINK_MAP": "y"}
    forbidden = {"ESP32S3_M5_CORES3_LCD", "ESPRESSIF_WIFI", "ESPRESSIF_BLE"}
    microphone = profile == "cores3-microphone"
    if microphone:
        required.update(PINS)
        required.update({key: "y" for key in (
            "ESP32S3_M5_CORES3_MIC", "AUDIO_ES7210", "AUDIO", "SYSTEM_NXRECORDER",
            "ESPRESSIF_I2S0_RX", "ESPRESSIF_I2S0_MCLK", "ESPRESSIF_I2S0_ROLE_MASTER")})
        required.update({"ESPRESSIF_I2S0_SAMPLE_RATE": "16000",
                         "ES7210_BUFFER_SIZE": "1024", "ES7210_NUM_BUFFERS": "4"})
        forbidden.update({"ESPRESSIF_I2S0_TX", "ESPRESSIF_I2S1", "AUDIO_EXCLUDE_STOP",
                          "AUDIO_MULTI_SESSION", "ESP32S3_SPIRAM"})
    else:
        forbidden.update({"ESP32S3_M5_CORES3_MIC", "AUDIO_ES7210", "SYSTEM_NXRECORDER"})
    for key, value in required.items():
        actual = config.get("CONFIG_" + key)
        if actual != value:
            raise ValueError(f"CONFIG_{key}: expected {value}, got {actual}")
    for key in forbidden:
        if config.get("CONFIG_" + key, "n") != "n":
            raise ValueError(f"unexpected enabled setting: CONFIG_{key}")
    # nm is called with --defined-only, so undefined references do not count.
    names = {line.split()[-1] for line in nm_output.splitlines() if line.split()}
    actual = SYMBOLS & names
    expected = SYMBOLS if microphone else set()
    if actual != expected:
        raise ValueError(f"linked audio symbols: expected {sorted(expected)}, got {sorted(actual)}")
    return {
        "profile": profile,
        "microphone_linked": microphone,
        "physical_capture_verified": False,
        "sha256": {str(path.relative_to(out)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in paths},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--nm", default="xtensa-esp32s3-elf-nm")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        parser.error("source-sha must be a full Git commit SHA")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if actual != args.source_sha:
        raise SystemExit(f"checkout identity mismatch: expected {args.source_sha}, got {actual}")
    symbols = subprocess.check_output([args.nm, "--defined-only", str(args.out / "nuttx/nuttx")], text=True)
    record = verify(args.out, args.profile, symbols)
    record["source_sha"] = actual
    record["nuttx_revision"] = json.loads((ROOT / "upstream/nuttx/release.json").read_text())["revision"]
    text = json.dumps(record, indent=2) + "\n"
    (args.out / "audio-build-evidence.json").write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
