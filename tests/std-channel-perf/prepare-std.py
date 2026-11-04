#!/usr/bin/env python3
"""Prepare a private NuttX std with one exact, test-only channel witness hook.

Latency builds keep the channel implementation byte-identical to the pinned SDK.
All modes retain the common NuttX ABI correctness patches used by ordinary nxrs
firmware. No installed SDK or shared source tree is modified.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LIBRARY = Path("lib/rustlib/src/rust/library")
ARRAY = LIBRARY / "std/src/sync/mpmc/array.rs"
HOOK = "nxrs_std_channel_test_hook"
MODES = {"latency": 0, "witness-reserved": 1, "witness-published": 2}
# Independently checked in upstream Rust 1.90.0, the checksum-pinned ESP
# rust-src-1.90.0.0 archive, and nightly-2025-09-15's installed rust-src.
PINS = {
    sdk: {
        "git_blob_sha1": "880d8b5f57cf4ef1c2a55a54e92c24fc86879489",
        "sha256": "3b7cce4fa348ff357658c44fa9def9bffe7ed6ff97a6ab4be74da3a0350e804c",
    }
    for sdk in ("nightly-2025-09-15", "esp-1.90.0.0")
}
IMPORT = "use crate::time::Instant;\n"
DECLARATION = (
    '\n// Private nxrs qualification image only; absent from latency images.\n'
    '#[cfg(target_os = "nuttx")]\n'
    'unsafe extern "C" {\n'
    f'    fn {HOOK}(stage: u32);\n'
    '}\n'
)
RESERVED = (
    "                    Ok(_) => {\n"
    "                        // Prepare the token for the follow-up call to `write`.\n"
)
PUBLISHED = (
    "            slot.stamp.store(token.array.stamp, Ordering::Release);\n"
    "        }\n\n"
    "        // Wake a sleeping receiver.\n"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def identity(data: bytes) -> dict:
    return {"git_blob_sha1": blob(data), "sha256": digest(data)}


def verify_preimage(data: bytes, sdk: str) -> None:
    if sdk not in PINS or identity(data) != PINS[sdk]:
        raise ValueError("unexpected std mpmc array preimage; refuse SDK/source drift")


def transform(data: bytes, mode: str) -> bytes:
    """Structural transformation; callers must pin the complete preimage first."""
    if mode not in MODES:
        raise ValueError("unknown std-channel image mode")
    text = data.decode("utf-8")
    if HOOK in text:
        raise ValueError("std source already contains the channel witness hook")
    if mode == "latency":
        return data
    if text.count(IMPORT) != 1 or text.count(RESERVED) != 1 or text.count(PUBLISHED) != 1:
        raise ValueError("ambiguous or missing reservation/publication anchors")
    if not text.index(RESERVED) < text.index(PUBLISHED):
        raise ValueError("reservation/publication source order changed")
    stage = MODES[mode]
    if stage == 1:
        insertion = (
            "                    Ok(_) => {\n"
            '                        #[cfg(target_os = "nuttx")]\n'
            "                        // SAFETY: this private image links the fixture's Rust hook.\n"
            "                        unsafe {\n"
            f"                            {HOOK}(1);\n"
            "                        }\n"
            "                        // Prepare the token for the follow-up call to `write`.\n"
        )
        text = text.replace(RESERVED, insertion, 1)
    else:
        insertion = (
            "            slot.stamp.store(token.array.stamp, Ordering::Release);\n"
            "        }\n\n"
            '        #[cfg(target_os = "nuttx")]\n'
            "        // SAFETY: this private image links the fixture's Rust hook.\n"
            "        unsafe {\n"
            f"            {HOOK}(2);\n"
            "        }\n\n"
            "        // Wake a sleeping receiver.\n"
        )
        text = text.replace(PUBLISHED, insertion, 1)
    return text.replace(IMPORT, IMPORT + DECLARATION, 1).encode("utf-8")


def checked_transform(data: bytes, sdk: str, mode: str) -> bytes:
    verify_preimage(data, sdk)
    return transform(data, mode)


def contained_file(root: Path, relative: Path) -> Path:
    root = root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"source path escapes private sysroot: {relative}")
    return path


def source_inventory(library: Path) -> dict[str, str]:
    library = library.resolve(strict=True)
    result = {}
    for path in sorted(library.rglob("*")):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(library):
            raise ValueError(f"std source symlink escapes library: {path}")
        if path.is_file():
            result[path.relative_to(library).as_posix()] = digest(path.read_bytes())
    if not result:
        raise ValueError("empty std source inventory")
    return result


def inventory_digest(inventory: dict[str, str]) -> str:
    return digest(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode())


def compiler_identity(sysroot: Path) -> dict:
    compiler = contained_file(sysroot, Path("bin/rustc"))
    version = subprocess.check_output([str(compiler), "--version", "--verbose"], text=True)
    reported = Path(subprocess.check_output(
        [str(compiler), "--print", "sysroot"], text=True).strip()).resolve(strict=True)
    if reported != sysroot.resolve(strict=True):
        raise ValueError("compiler reports a different sysroot")
    # SDK preparation/build tools can leave atomic-copy scratch files beside
    # the compiler (for example .librustc_driver-HASH.so.RANDOM).  They are not
    # loaded by rustc and must not change the compiler's recorded identity.
    drivers = sorted(p for pattern in ("librustc_driver-*.so", "librustc_driver-*.dylib")
                     for p in (sysroot / "lib").glob(pattern))
    return {
        "version": version.strip(),
        "rustc_sha256": digest(compiler.read_bytes()),
        "driver_sha256": {p.name: digest(p.read_bytes()) for p in drivers if p.is_file()},
    }


def prepare_common_sdk(source: Path, output: Path, sdk: str) -> None:
    subprocess.run([
        sys.executable, str(ROOT / "tests/nuttx-std/prepare-std.py"),
        "--source", str(source), "--output", str(output), "--sdk", sdk,
    ], cwd=ROOT, check=True)


def validate_receipt(output: Path, mode: str | None = None) -> dict:
    """Check actual selected bytes; a caller-supplied receipt is not evidence."""
    output = output.resolve(strict=True)
    record = json.loads((output / "std-channel-hook.json").read_text())
    if record.get("schema") != "std-channel-hook-v1" or record.get("mode") not in MODES:
        raise ValueError("invalid std-channel hook receipt")
    if mode is not None and record["mode"] != mode:
        raise ValueError("std-channel hook receipt mode mismatch")
    if record.get("hook_stage") != MODES[record["mode"]]:
        raise ValueError("std-channel hook stage mismatch")
    sdk = record.get("sdk")
    if sdk not in PINS or record.get("original") != PINS[sdk]:
        raise ValueError("std-channel hook receipt preimage mismatch")
    # The exact original is retained as evidence outside the compiled library.
    original = contained_file(output, Path("array-original.rs")).read_bytes()
    expected = checked_transform(original, sdk, record["mode"])
    sysroot = output / "toolchain"
    actual = contained_file(sysroot, ARRAY).read_bytes()
    if actual != expected or record.get("selected") != identity(actual):
        raise ValueError("selected std array does not match the pinned image mode")
    files = source_inventory(sysroot / LIBRARY)
    if record.get("library_tree_sha256") != inventory_digest(files):
        raise ValueError("selected std library changed since preparation")
    if json.loads((output / "std-library-sources.json").read_text()) != files:
        raise ValueError("selected std source inventory mismatch")
    if compiler_identity(sysroot) != record.get("compiler"):
        raise ValueError("selected compiler changed since preparation")
    return record


def prepare(source: Path, output: Path, sdk: str, mode: str) -> dict:
    source = source.resolve(strict=True)
    output = output.absolute()
    if output.exists():
        raise ValueError("private preparation output must be new")
    if output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve()):
        raise ValueError("installed and private SDK paths must be disjoint")
    # Installed ESP SDK's library may be an external distribution symlink.
    original = (source / ARRAY).read_bytes()
    changed = checked_transform(original, sdk, mode)
    original_compiler = compiler_identity(source)
    output.mkdir(parents=True)
    prepare_common_sdk(source, output, sdk)
    sysroot = output / "toolchain"
    target = contained_file(sysroot, ARRAY)
    if target.read_bytes() != original or target.stat().st_nlink != 1:
        raise ValueError("private array source is not an independent original copy")
    if mode != "latency":
        target.write_bytes(changed)
        patch = "".join(difflib.unified_diff(
            original.decode().splitlines(True), changed.decode().splitlines(True),
            fromfile=str(ARRAY), tofile=str(ARRAY)))
        (output / "std-channel-witness.patch").write_text(patch)
    if (source / ARRAY).read_bytes() != original or compiler_identity(source) != original_compiler:
        raise ValueError("installed SDK changed during private preparation")
    (output / "array-original.rs").write_bytes(original)
    files = source_inventory(sysroot / LIBRARY)
    (output / "std-library-sources.json").write_text(json.dumps(files, indent=2) + "\n")
    record = {
        "schema": "std-channel-hook-v1", "sdk": sdk, "mode": mode,
        "hook_stage": MODES[mode], "source_path": ARRAY.as_posix(),
        "original": identity(original), "selected": identity(changed),
        "library_tree_sha256": inventory_digest(files),
        "compiler": compiler_identity(sysroot),
        "installed_array_unchanged": True,
        "channel_instrumented": mode != "latency",
        "common_abi_patches": "std-patch.json",
        "note": "Private qualification image; witness timing is not latency evidence.",
    }
    (output / "std-channel-hook.json").write_text(json.dumps(record, indent=2) + "\n")
    # Existing generic builder copies this ABI ledger unchanged into its receipt.
    ledger_path = output / "std-patch.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["channel_qualification"] = record
    if mode != "latency":
        name = "std-channel-witness.patch"
        ledger["patchset_sha256"][name] = digest((output / name).read_bytes())
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n")
    return validate_receipt(output, mode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sdk", required=True, choices=tuple(PINS))
    parser.add_argument("--mode", required=True, choices=tuple(MODES))
    args = parser.parse_args()
    try:
        result = prepare(args.source, args.output, args.sdk, args.mode)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"private std preparation failed: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
