#!/usr/bin/env python3
"""Package one diagnostic recording and its exact ELF into a portable session.

The viewer still validates map placement, memory geometry and clock correlation.
Packaging never establishes running-target identity for an unbound console log.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
MIB = 1024 * 1024
MAX_SESSION_BYTES = 1024 * MIB
MAX_SNAPSHOTS = 128
LIMITS = {"elf": 512 * MIB, "console": 128 * MIB, "map": 64 * MIB,
          "layout": 8 * MIB, "snapshot": 32 * MIB, "heap_journal": 32 * MIB,
          "trace": 128 * MIB}


def _converter():
    specification = importlib.util.spec_from_file_location("nxrs_trace_converter", ROOT.parent / "nuttx-trace-to-perfetto.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _discovery():
    specification = importlib.util.spec_from_file_location("nxrs_layout_discovery", ROOT / "layout_discovery.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _copy(source, destination, limit):
    """Copy a stable regular file once, hashing the actual bytes being retained."""
    source = Path(source)
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Input must be a regular file, not a symlink or directory: {source}")
    if not 0 < before.st_size <= limit:
        raise ValueError(f"Input {source} must contain 1..{limit} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(os.open(source, flags), "rb") as stream, destination.open("xb") as output:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"Input changed while opening: {source}")
        for block in iter(lambda: stream.read(MIB), b""):
            size += len(block)
            if size > limit:
                raise ValueError(f"Input exceeds its {limit}-byte bound: {source}")
            digest.update(block)
            output.write(block)
        after = os.fstat(stream.fileno())
        if size != before.st_size or after.st_mtime_ns != before.st_mtime_ns or after.st_size != before.st_size:
            raise ValueError(f"Input changed while copying: {source}")
    return {"sha256": digest.hexdigest(), "bytes": size}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _json(path):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=reject_constant)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError(f"{path.name}: expected an object with schema_version 1")
    return value


def _hash_matches(value, expected, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value) or value.lower() != expected:
        raise ValueError(f"{label}: ELF SHA-256 is missing, invalid, or does not match firmware.elf")


def _validate_memory_identity(path, expected, kind):
    value = _json(path)
    build = value.get("build")
    if not isinstance(build, dict):
        raise ValueError(f"{path.name}: missing build identity")
    _hash_matches(build.get("elf_sha256"), expected, path.name)
    capture = value.get("capture")
    if isinstance(capture, dict) and "elf_sha256" in capture:
        _hash_matches(capture["elf_sha256"], expected, f"{path.name} capture")
    collection = "snapshots" if kind == "snapshot" else "records"
    if not isinstance(value.get(collection), list):
        raise ValueError(f"{path.name}: missing {collection} array")
    return value


def _publish(staging, output):
    """Atomically rename without replacing even an empty existing destination."""
    if os.name == "nt":
        os.rename(staging, output)  # Windows rename refuses any existing target.
        return
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes, output_bytes = os.fsencode(staging), os.fsencode(output)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(-100, source_bytes, -100, output_bytes, 1)  # AT_FDCWD, RENAME_NOREPLACE.
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(source_bytes, output_bytes, 4)  # RENAME_EXCL.
    else:
        raise OSError("Atomic directory publication without replacement is unavailable on this host")
    if status:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(output))


def prepare(*, elf, console, output, map_path=None, layout=None, snapshots=(), heap_journal=None,
            board=None, addr2line=None):
    """Build session.json and return its path only after all artifacts are published."""
    snapshots = list(snapshots)
    if len(snapshots) > MAX_SNAPSHOTS:
        raise ValueError(f"At most {MAX_SNAPSHOTS} snapshot files can enter one session")
    if board and layout:
        raise ValueError("Choose either --board or --layout")
    output = Path(os.path.abspath(output))
    if os.path.lexists(output):
        raise FileExistsError(errno.EEXIST, "Refusing an existing session output", str(output))
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    if not parent.is_dir():
        raise ValueError("The session output parent must be an existing directory")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-preparing-", dir=parent))
    try:
        files = {}
        total = 0

        def retain(source, name, kind):
            nonlocal total
            entry = _copy(source, staging / name, LIMITS[kind])
            total += entry["bytes"]
            if total > MAX_SESSION_BYTES:
                raise ValueError("Session exceeds the 1 GiB combined input/output bound")
            files[name] = entry
            return name

        elf_name = retain(elf, "firmware.elf", "elf")
        console_name = retain(console, "console.txt", "console")
        identity = files[elf_name]["sha256"]
        discovered = _discovery().discover(elf, identity, map_path=map_path, layout=layout, board=board)
        map_path, layout = discovered["map"], discovered["layout"]
        manifest = {"schema_version": 1, "build": {"elf_sha256": identity, "label": Path(elf).name},
                    "elf": elf_name, "snapshots": [], "files": files,
                    "discovery": {"warnings": discovered["warnings"]}}
        if map_path:
            manifest["map"] = retain(map_path, "firmware.map", "map")
            manifest["automatic_map"] = discovered["automatic_map"]
        if layout:
            if discovered["layout_data"] is None:
                manifest["layout"] = retain(layout, "layout.json", "layout")
            else:
                encoded = (json.dumps(discovered["layout_data"], indent=2, ensure_ascii=True) + "\n").encode("utf-8")
                if len(encoded) > LIMITS["layout"] or total + len(encoded) > MAX_SESSION_BYTES:
                    raise ValueError("Generated layout exceeds its input or session byte bound")
                (staging / "layout.json").write_bytes(encoded)
                files["layout.json"] = {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
                total += len(encoded)
                manifest["layout"] = "layout.json"
            manifest["automatic_layout"] = discovered["automatic_layout"]
            description = _json(staging / manifest["layout"])
            if not isinstance(description.get("regions"), list):
                raise ValueError("layout.json: missing regions array")
            if isinstance(description.get("build"), dict) and "elf_sha256" in description["build"]:
                _hash_matches(description["build"]["elf_sha256"], identity, "layout.json")
        for index, path in enumerate(snapshots, 1):
            name = retain(path, f"snapshot-{index:04}.json", "snapshot")
            _validate_memory_identity(staging / name, identity, "snapshot")
            manifest["snapshots"].append(name)
        if heap_journal:
            name = retain(heap_journal, "heap-journal.json", "heap_journal")
            _validate_memory_identity(staging / name, identity, "heap_journal")
            manifest["heap_journal"] = name
        converter = _converter()
        with (staging / console_name).open(encoding="utf-8", errors="strict") as stream:
            trace = converter.convert(stream, elf_sha256=identity)
        frames = trace["otherData"]["nxrs"].get("capture_frames", {})
        for endpoint in ("begin", "end"):
            if "elf_sha256" in frames.get(endpoint, {}):
                _hash_matches(frames[endpoint]["elf_sha256"], identity, f"console {endpoint} frame")
        converter.symbolize_trace(trace, staging / elf_name, addr2line=addr2line)
        trace["otherData"]["nxrs"]["warnings"].append(
            "Session packaging binds the selected files to the supplied ELF. Unless captured firmware identity was independently verified, the console-to-ELF association is a producer assertion.")
        trace["otherData"]["nxrs"]["warnings"].extend(discovered["warnings"])
        trace_path = staging / "trace.json"
        encoded = (converter.encode_trace(trace) + "\n").encode("utf-8")
        if len(encoded) > LIMITS["trace"] or total + len(encoded) > MAX_SESSION_BYTES:
            raise ValueError("Converted trace or complete session exceeds its byte bound")
        if len(trace["traceEvents"]) > 1_000_000:
            raise ValueError("Converted trace exceeds the viewer's one million event bound")
        trace_path.write_bytes(encoded)
        files[trace_path.name] = {"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
        manifest["trace"] = trace_path.name
        (staging / "session.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        _publish(staging, output)
        return output / "session.json"
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True, help="Exact unstripped final ET_EXEC firmware ELF")
    parser.add_argument("--console", type=Path, required=True, help="Saved NuttX noteram console log")
    parser.add_argument("--output", type=Path, required=True, help="New session directory; parent must exist")
    parser.add_argument("--map", dest="map_path", type=Path, help="GNU linker map from the same final link; otherwise discover an adjacent map")
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument("--layout", type=Path, help="Override automatic discovery with a board layout JSON")
    layout.add_argument("--board", choices=[entry["id"] for entry in _discovery().catalogue()], help="Select a bundled board when build metadata is unavailable")
    parser.add_argument("--snapshot", dest="snapshots", action="append", type=Path, default=[])
    parser.add_argument("--heap-journal", type=Path)
    parser.add_argument("--addr2line", help="Target addr2line executable for source resolution")
    args = parser.parse_args()
    try:
        manifest = prepare(**vars(args))
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(manifest)
    print("Open with: nxrs-memory-explorer --session " + shlex.quote(str(manifest)))


if __name__ == "__main__":
    main()
