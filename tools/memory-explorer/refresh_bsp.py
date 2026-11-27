#!/usr/bin/env python3
"""Verify or restore the exact BSP reference files bundled with the viewer.

This never evaluates linker scripts, preprocesses headers, executes BSP code,
selects a newer revision, or changes the reviewed source manifest. Updating a
pin is a source review followed by a catalogue/geometry review, not a download.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import urllib.request


LAYOUTS = Path(__file__).resolve().parent / "layouts"
REFERENCES = Path(__file__).resolve().parents[2] / "upstream" / "memory-layouts"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def read_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular, nonsymlink file: {path}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"Oversized metadata: {path}")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def local_path(root, name):
    if not isinstance(name, str) or not name or "\\" in name or name != name.strip():
        raise ValueError("Invalid local source path")
    pure = PurePosixPath(name)
    if pure.is_absolute() or str(pure) != name or any(part in (".", "..") for part in pure.parts):
        raise ValueError(f"Unsafe local source path: {name}")
    if any(ord(character) < 32 for character in name):
        raise ValueError("Control character in source path")
    current = root
    if current.is_symlink():
        raise ValueError(f"Symlinked layout root: {root}")
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlinked source path: {current}")
    return current


def load_manifest(root):
    manifest = read_json(root / "sources.json")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("sources"), list):
        raise ValueError("Unsupported source manifest")
    pins = manifest.get("pins", {})
    pinned = set()
    for pin in pins.values():
        repository, revision = pin.get("repository", ""), pin.get("revision", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid source repository")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Sources must use complete immutable commit IDs")
        pinned.add((repository, revision))
    seen, total = set(), 0
    for item in manifest["sources"]:
        if (item.get("repository"), item.get("revision")) not in pinned:
            raise ValueError("Source does not match a reviewed manifest pin")
        local = item.get("local", "")
        local_path(root, local)
        if not local.startswith("sources/") or local in seen:
            raise ValueError("Source paths must be unique and inside sources/")
        seen.add(local)
        source = item.get("path", "")
        local_path(root, source)
        expected_url = (
            "https://raw.githubusercontent.com/"
            + item["repository"] + "/" + item["revision"] + "/" + source
        )
        if item.get("url") != expected_url:
            raise ValueError("Source URL must name the exact reviewed repository, revision and path")
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise ValueError("Invalid SHA-256 in source manifest")
        size = item.get("bytes")
        if type(size) is not int or not 0 < size <= MAX_SOURCE_BYTES:
            raise ValueError("Invalid or oversized source length")
        total += size
    if not seen or total > MAX_TOTAL_BYTES:
        raise ValueError("Empty or oversized source inventory")
    return manifest


def checked_bytes(item, data):
    if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"Pinned source differs: {item['local']}")
    return data


def render_rust_catalog(catalog):
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = [
        "//! Generated from layouts/catalog.json; verify with refresh_bsp.py --check.",
        "//! Bundled BSP data is read offline and is never executed.",
        "use super::BuiltinLayout;", "",
        "pub const SUPPORTED_LAYOUT_BOARDS: &[(&str, &str)] = &[",
    ]
    for board in catalog["boards"]:
        pair = f"({quote(board['id'])}, {quote(board['label'])})"
        # Match the pinned rustfmt tuple-width rule so generated data passes
        # the same formatting gate as the surrounding Rust implementation.
        if len(pair) > 60:
            lines.extend(["    (", f"        {quote(board['id'])},",
                          f"        {quote(board['label'])},", "    ),"])
        else:
            lines.append(f"    {pair},")
    lines.extend(["];", "", "pub(super) const BUILTIN_LAYOUTS: &[BuiltinLayout] = &["])
    for board in catalog["boards"]:
        lines.append("    BuiltinLayout {")
        for key in ("id", "label", "nuttx_board", "nuttx_revision"):
            lines.append(f"        {key}: {quote(board[key])},")
        lines.append("        required_config: &[")
        for key, value in board["required_config"].items():
            lines.append(f"            ({quote(key)}, {quote(value)}),")
        lines.extend([
            "        ],", f"        json: include_str!(\"../layouts/{board['layout']}\"),", "    },",
        ])
    lines.extend(["];", ""])
    return "\n".join(lines)


def check_catalog(root, manifest):
    catalog = read_json(root / "catalog.json")
    if catalog.get("schema_version") != 1 or not catalog.get("boards"):
        raise ValueError("Unsupported or empty BSP catalogue")
    ids, boards = set(), set()
    sources = {item["url"] for item in manifest["sources"]}
    for board in catalog["boards"]:
        if not re.fullmatch(r"[a-z0-9-]+", board.get("id", "")) or board["id"] in ids:
            raise ValueError("Invalid or duplicate board selector")
        ids.add(board["id"])
        if board["nuttx_board"] in boards:
            raise ValueError("Duplicate native BSP selector")
        boards.add(board["nuttx_board"])
        if board["nuttx_revision"] != manifest["pins"]["nuttx"]["revision"]:
            raise ValueError("Board and source NuttX revisions differ")
        if not board.get("required_config") or any(
            not re.fullmatch(r"CONFIG_[A-Z0-9_]+", key) or not isinstance(value, str)
            for key, value in board["required_config"].items()
        ):
            raise ValueError("Invalid effective-configuration selector")
        layout_path = local_path(root, board["layout"])
        if layout_path.parent != root or layout_path.suffix != ".json":
            raise ValueError("A layout must be a JSON file directly inside layouts/")
        layout = read_json(layout_path)
        if layout.get("schema_version") != 1 or not layout.get("physical_resources"):
            raise ValueError("Layout has no admitted physical storage")
        qualification = layout.get("qualification", {})
        if qualification.get("nuttx_revision") != board["nuttx_revision"]:
            raise ValueError("Descriptor and catalogue revisions differ")
        for url in qualification.get("sources", []):
            raw = url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
            if raw not in sources:
                raise ValueError(f"Descriptor cites an unretained source: {url}")
    table = root.parent / "src" / "layout_catalog.rs"
    if table.is_symlink() or table.read_text(encoding="utf-8") != render_rust_catalog(catalog):
        raise ValueError("Rust layout table differs from layouts/catalog.json")
    return catalog


def check(layouts=LAYOUTS, references=REFERENCES):
    manifest = load_manifest(references)
    catalog = check_catalog(layouts, manifest)
    expected = set()
    for item in manifest["sources"]:
        path = local_path(references, item["local"])
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError(f"Missing or oversized source: {item['local']}")
        checked_bytes(item, path.read_bytes())
        expected.add(item["local"])
    actual = set()
    for path in (references / "sources").rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink in source inventory: {path}")
        if path.is_file():
            actual.add(path.relative_to(references).as_posix())
    if actual != expected:
        raise ValueError("Source inventory contains missing or unlisted files")
    return {"boards": len(catalog["boards"]), "sources": len(expected),
            "bytes": sum(item["bytes"] for item in manifest["sources"])}


def download(layouts=LAYOUTS, references=REFERENCES):
    manifest = load_manifest(references)
    check_catalog(layouts, manifest)

    def retrieve(item):
        request = urllib.request.Request(item["url"], headers={"User-Agent": "nxrs-bsp-reference-verifier"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
        return item, checked_bytes(item, data)

    # Validate every response before writing any source file. A mismatching
    # remote response must not partly refresh the checked-in reference set.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(retrieve, manifest["sources"]))
    for item, data in results:
        path = local_path(references, item["local"])
        if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        local_path(references, item["local"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return check(layouts, references)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Verify bundled sources and catalogue offline")
    action.add_argument("--download", action="store_true", help="Restore exact pinned sources after SHA-256 verification")
    args = parser.parse_args()
    try:
        result = download() if args.download else check()
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
