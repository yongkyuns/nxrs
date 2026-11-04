#!/usr/bin/env python3
"""Prepare hash-pinned, private Crossbeam progress witnesses.

Only newly extracted copies are instrumented. Installed Cargo sources, production
dependencies and the ordinary std measurement fixture are never written. The
complete original archives remain beside the selected source, so validation can
reconstruct every byte without trusting a caller-supplied source inventory.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HOOK = "nxrs_crossbeam_progress_hook"
MODES = {"clean": 0, "send-reserved": 1, "send-published": 2,
         "recv-reserved": 3, "recv-reclaimed": 4, "fifo-ordered": 5}
PINS = {
    "crossbeam-channel": {
        "version": "0.5.17",
        "sha256": "98b0cc327b5bc766e7fda9c9260cc0fa81b43a8e240440422dff70788e3f9ef1",
    },
    "crossbeam-utils": {
        "version": "0.8.23",
        "sha256": "a31eee39dddec8330830986fcd7625edb5a24ec90ea038215273bbc3adb08ac6",
    },
}
ARRAY = "src/flavors/array.rs"
SELECT = "src/select.rs"
REGISTRATION_BASE = 0x100
FIFO_MARKERS = {
    "send_reserved": 0x301,
    "send_published": 0x302,
    "recv_reserved": 0x303,
    "recv_reclaimed": 0x304,
    "selection_wait_returned": 0x200,
}
DECLARATION = (
    '\n// Private nxrs progress witness only; never a production dependency.\n'
    '#[cfg(target_os = "nuttx")]\n'
    'unsafe extern "C" {\n'
    f'    fn {HOOK}(stage: u32);\n'
    '}\n'
)
ARRAY_IMPORT = "use std::time::Instant;\n"
SELECT_IMPORT = "use crate::utils;\n"
RESERVE_SEND = (
    "                    Ok(_) => {\n"
    "                        // Prepare the token for the follow-up call to `write`.\n"
)
RESERVE_RECV = RESERVE_SEND.replace("`write`", "`read`")
PUBLISH_SEND = (
    "        slot.stamp.store(token.array.stamp, Ordering::Release);\n\n"
    "        // Wake a sleeping receiver.\n"
)
RECLAIM_RECV = PUBLISH_SEND.replace("receiver", "sender")
SELECT_WAIT = (
    "                // Block the current thread.\n"
    "                sel = cx.wait_until(deadline);\n"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(files: dict[str, bytes]) -> dict[str, str]:
    return {name: digest(data) for name, data in sorted(files.items())}


def inventory_digest(files: dict[str, str]) -> str:
    return digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())


def verify_lock(lock: Path = ROOT / "Cargo.lock") -> None:
    packages = tomllib.loads(lock.read_text())["package"]
    for name, pin in PINS.items():
        matching = [p for p in packages if p["name"] == name]
        if len(matching) != 1 or matching[0].get("version") != pin["version"] \
                or matching[0].get("checksum") != pin["sha256"] \
                or matching[0].get("source") != "registry+https://github.com/rust-lang/crates.io-index":
            raise ValueError(f"workspace lock does not match pinned {name}")


def archive_files(data: bytes, name: str) -> dict[str, bytes]:
    """Authenticate the complete crate before inspecting or extracting it."""
    pin = PINS[name]
    if digest(data) != pin["sha256"]:
        raise ValueError(f"unexpected {name} archive checksum; refuse source drift")
    prefix = f"{name}-{pin['version']}"
    result: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name \
                    or not path.parts or path.parts[0] != prefix:
                raise ValueError("crate archive contains an unsafe path")
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) < 2:
                raise ValueError("crate archive contains a link or nonregular file")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in result or relative in (".cargo-checksum.json", ".cargo-ok"):
                raise ValueError("crate archive contains duplicate or cache-generated files")
            total += member.size
            if total > 16 * 1024 * 1024 or len(result) >= 2048 or member.size < 0:
                raise ValueError("crate archive exceeds preparation bounds")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("crate archive file cannot be read")
            result[relative] = stream.read()
    manifest = tomllib.loads(result.get("Cargo.toml", b"").decode())
    package = manifest.get("package", {})
    if package.get("name") != name or package.get("version") != pin["version"]:
        raise ValueError("crate manifest does not match pinned identity")
    return result


def call(stage: str, indent: str) -> str:
    return (
        f'{indent}#[cfg(target_os = "nuttx")]\n'
        f"{indent}// SAFETY: the isolated witness links the fixture's Rust hook.\n"
        f"{indent}unsafe {{ {HOOK}({stage}); }}\n"
    )


def transform_array(data: bytes, mode: str) -> bytes:
    if mode not in MODES:
        raise ValueError("unknown Crossbeam image mode")
    text = data.decode()
    if HOOK in text:
        raise ValueError("Crossbeam array is already instrumented")
    if mode == "clean":
        return data
    anchors = (ARRAY_IMPORT, RESERVE_SEND, PUBLISH_SEND, RESERVE_RECV, RECLAIM_RECV)
    if any(text.count(anchor) != 1 for anchor in anchors):
        raise ValueError("missing or ambiguous Crossbeam array hook anchors")
    positions = [text.index(anchor) for anchor in anchors[1:]]
    if positions != sorted(positions):
        raise ValueError("Crossbeam array operation order changed")
    if mode == "fifo-ordered":
        # One isolated image records both sides of each actual slot interval.
        # The runtime chooses which boundary posts its native observer; neither
        # these calls nor the recipe introduce a reservation-holding wait.
        for anchor, marker, reserved in (
            (RESERVE_SEND, "send_reserved", True),
            (PUBLISH_SEND, "send_published", False),
            (RESERVE_RECV, "recv_reserved", True),
            (RECLAIM_RECV, "recv_reclaimed", False),
        ):
            if reserved:
                first, remainder = anchor.split("\n", 1)
                replacement = first + "\n" + call(hex(FIFO_MARKERS[marker]), " " * 24) + remainder
            else:
                first, remainder = anchor.split("\n\n", 1)
                replacement = first + "\n\n" + call(
                    hex(FIFO_MARKERS[marker]), " " * 8) + "\n" + remainder
            text = text.replace(anchor, replacement, 1)
        return text.replace(ARRAY_IMPORT, ARRAY_IMPORT + DECLARATION, 1).encode()
    stage = MODES[mode]
    if stage in (1, 3):
        anchor = RESERVE_SEND if stage == 1 else RESERVE_RECV
        first, remainder = anchor.split("\n", 1)
        replacement = first + "\n" + call(str(stage), " " * 24) + remainder
    else:
        anchor = PUBLISH_SEND if stage == 2 else RECLAIM_RECV
        first, remainder = anchor.split("\n\n", 1)
        replacement = first + "\n\n" + call(str(stage), " " * 8) + "\n" + remainder
    return text.replace(anchor, replacement, 1).replace(
        ARRAY_IMPORT, ARRAY_IMPORT + DECLARATION, 1).encode()


def transform_select(data: bytes, mode: str) -> bytes:
    if mode not in MODES:
        raise ValueError("unknown Crossbeam image mode")
    text = data.decode()
    if HOOK in text:
        raise ValueError("Crossbeam select is already instrumented")
    if mode == "clean":
        return data
    if text.count(SELECT_IMPORT) != 1 or text.count("\nfn run_select(") != 1 \
            or text.count("\nfn run_ready(") != 1:
        raise ValueError("missing or ambiguous Crossbeam select function anchors")
    start = text.index("\nfn run_select(")
    end = text.index("\nfn run_ready(")
    selected = text[start:end]
    if start >= end or selected.count(SELECT_WAIT) != 1 \
            or selected.count("registered_count += 1;") != 1:
        raise ValueError("Crossbeam selection registration/wait structure changed")
    # This encodes the count that run_select actually registered, before its
    # Context wait. A callback is not proof of parking: the fixture separately
    # requires a target TCB WAIT_SEM observation while this call is outstanding.
    replacement = call("0x100 + registered_count as u32", " " * 16) + SELECT_WAIT
    if mode == "fifo-ordered":
        replacement += call(hex(FIFO_MARKERS["selection_wait_returned"]), " " * 16)
    selected = selected.replace(SELECT_WAIT, replacement, 1)
    text = text[:start] + selected + text[end:]
    return text.replace(SELECT_IMPORT, SELECT_IMPORT + DECLARATION, 1).encode()


def selected_files(original: dict[str, bytes], name: str, mode: str) -> dict[str, bytes]:
    selected = dict(original)
    if name == "crossbeam-channel":
        selected[ARRAY] = transform_array(original[ARRAY], mode)
        selected[SELECT] = transform_select(original[SELECT], mode)
    return selected


def source_files(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("private source directory is missing or a symlink")
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("private source contains a symlink")
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError("private source is not an independent copy")
            result[path.relative_to(directory).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError("private source contains a nonregular file")
    return result


def config_bytes(output: Path) -> bytes:
    lines = ["# Private progress witness; supply only to its isolated Cargo build.",
             "[patch.crates-io]"]
    for name, pin in PINS.items():
        path = output / "sources" / f"{name}-{pin['version']}"
        lines.append(f"{name} = {{ path = {json.dumps(str(path))} }}")
    return ("\n".join(lines) + "\n").encode()


def expected_receipt(output: Path, mode: str, archives: dict[str, bytes]) -> dict:
    if mode not in MODES:
        raise ValueError("unknown Crossbeam image mode")
    crates = {}
    patches = {}
    for name, data in archives.items():
        pin = PINS[name]
        original = archive_files(data, name)
        selected = selected_files(original, name, mode)
        before, after = inventory(original), inventory(selected)
        crates[name] = {
            "version": pin["version"], "archive_sha256": pin["sha256"],
            "archive": f"original/{name}-{pin['version']}.crate",
            "source_directory": f"sources/{name}-{pin['version']}",
            "original_tree_sha256": inventory_digest(before),
            "selected_tree_sha256": inventory_digest(after),
            "original_files": before, "selected_files": after,
        }
        for path in original:
            if original[path] != selected[path]:
                patch = "".join(difflib.unified_diff(
                    original[path].decode().splitlines(True),
                    selected[path].decode().splitlines(True),
                    fromfile=f"{name}/{path}", tofile=f"{name}/{path}")).encode()
                patches[f"{Path(path).stem}-witness.patch"] = digest(patch)
    record = {
        "schema": "crossbeam-progress-source-v1", "mode": mode,
        "hook_stage": MODES[mode], "instrumented": mode != "clean",
        "hook_symbol": HOOK if mode != "clean" else None,
        "selection_registration_hook": {
            "base": REGISTRATION_BASE, "value": "base + actual registered_count",
            "scope": "run_select-before-context-wait",
        } if mode != "clean" else None,
        "crates": crates,
        "patch_config_sha256": digest(config_bytes(output)),
        "patches_sha256": patches,
        "note": "Private source identity only; no execution, progress or timing claim.",
    }
    if mode == "fifo-ordered":
        record["fifo_order_hooks"] = {
            "markers": dict(FIFO_MARKERS),
            "scope": "array-slot-reservation-release-and-run_select-wait-return",
        }
    return record


def validate_receipt(output: Path, mode: str | None = None) -> dict:
    output = output.resolve(strict=True)
    record = json.loads((output / "crossbeam-hook.json").read_text())
    selected_mode = record.get("mode")
    if selected_mode not in MODES or (mode is not None and mode != selected_mode):
        raise ValueError("Crossbeam source receipt mode mismatch")
    archives = {}
    for name, pin in PINS.items():
        path = output / "original" / f"{name}-{pin['version']}.crate"
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output):
            raise ValueError("missing or escaped original crate archive")
        archives[name] = path.read_bytes()
    expected = expected_receipt(output, selected_mode, archives)
    if record != expected:
        raise ValueError("Crossbeam source receipt differs from authenticated reconstruction")
    for name, data in archives.items():
        original = archive_files(data, name)
        selected = selected_files(original, name, selected_mode)
        directory = output / record["crates"][name]["source_directory"]
        if (output / "sources").is_symlink() or not directory.resolve().is_relative_to(output):
            raise ValueError("private Crossbeam source escapes prepared output")
        actual = source_files(directory)
        if actual != selected:
            raise ValueError("private Crossbeam source changed since preparation")
    config = output / "cargo-config.toml"
    if config.is_symlink() or config.read_bytes() != config_bytes(output):
        raise ValueError("private Cargo patch configuration changed")
    for name, expected_hash in record["patches_sha256"].items():
        path = output / name
        if path.is_symlink() or digest(path.read_bytes()) != expected_hash:
            raise ValueError("private Crossbeam witness patch changed")
    return record


def prepare(channel_archive: Path, utils_archive: Path, output: Path, mode: str) -> dict:
    if mode not in MODES:
        raise ValueError("unknown Crossbeam image mode")
    output = output.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("private Crossbeam output must be new")
    archives = {name: path.resolve(strict=True).read_bytes() for name, path in (
        ("crossbeam-channel", channel_archive), ("crossbeam-utils", utils_archive))}
    # Authenticate all input bytes and all transformations before creating output.
    record = expected_receipt(output, mode, archives)
    output.mkdir(parents=True)
    (output / "original").mkdir()
    for name, data in archives.items():
        crate = record["crates"][name]
        (output / crate["archive"]).write_bytes(data)
        original = archive_files(data, name)
        selected = selected_files(original, name, mode)
        for path, content in selected.items():
            target = output / crate["source_directory"] / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for path in original:
            if original[path] != selected[path]:
                patch = "".join(difflib.unified_diff(
                    original[path].decode().splitlines(True),
                    selected[path].decode().splitlines(True),
                    fromfile=f"{name}/{path}", tofile=f"{name}/{path}"))
                (output / f"{Path(path).stem}-witness.patch").write_text(patch)
    (output / "cargo-config.toml").write_bytes(config_bytes(output))
    (output / "crossbeam-hook.json").write_text(json.dumps(record, indent=2) + "\n")
    return validate_receipt(output, mode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-archive", type=Path, required=True)
    parser.add_argument("--utils-archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    args = parser.parse_args()
    verify_lock()
    receipt = prepare(args.channel_archive, args.utils_archive, args.out, args.mode)
    print(json.dumps({"schema": receipt["schema"], "mode": receipt["mode"],
                      "hook_stage": receipt["hook_stage"],
                      "receipt": str(args.out / "crossbeam-hook.json")}, indent=2))


if __name__ == "__main__":
    main()
