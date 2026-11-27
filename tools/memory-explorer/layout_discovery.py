"""Find bounded, identity-matched build companions for portable recordings.

The board catalogue is bundled from pinned upstream sources. Build declarations
select it; an ELF address by itself never identifies a board or physical bank.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parent
MIB = 1024 * 1024


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _text(path, limit=8 * MIB):
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
        raise ValueError(f"{path} must be a regular file containing 1..{limit} bytes")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} exceeds its input bound")
    return data.decode("utf-8")


def _json(path):
    return json.loads(_text(path), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Non-finite JSON value {value}")))


def catalogue():
    path = ROOT / "layouts/catalog.json"
    data = _json(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("boards"), list):
        raise ValueError("Invalid bundled board catalogue")
    ids = set()
    for board in data["boards"]:
        if board["id"] in ids:
            raise ValueError("Duplicate bundled board ID")
        ids.add(board["id"])
        filename = board["layout"]
        if not isinstance(filename, str) or Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValueError("Bundled layout must name one flat filename")
    return data["boards"]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _receipt_hash(receipt, relative):
    relative = relative.as_posix()
    values = set()
    for line in receipt.splitlines():
        if "  " not in line:
            continue
        digest, path = line.split("  ", 1)
        path = path.replace("\\", "/")
        if path == relative or path.endswith("/" + relative):
            if not re.fullmatch("[0-9a-fA-F]{64}", digest):
                raise ValueError(f"Invalid SHA-256 for {relative} in binaries.sha256")
            values.add(digest.lower())
    if len(values) != 1:
        raise ValueError(f"binaries.sha256 has no unambiguous identity for {relative}")
    return values.pop()


def _configuration(text):
    values = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            key, value = line[2:-11], "n"
        else:
            continue
        if key in values:
            raise ValueError(f"Duplicate effective configuration key {key}")
        values[key] = value
    return values


def _context_at(root, elf, identity):
    provenance, hashes = root / "provenance.txt", root / "binaries.sha256"
    config_path = root / "resolved.config"
    if provenance.exists() or hashes.exists():
        receipt = _text(hashes)
        if _receipt_hash(receipt, elf.relative_to(root)) != identity:
            raise ValueError("binaries.sha256 belongs to a different ELF (SHA-256 mismatch)")
        config = _text(config_path)
        if _receipt_hash(receipt, Path("resolved.config")) != _sha(config.encode("utf-8")):
            raise ValueError("resolved.config does not match binaries.sha256")
        fields = {}
        for line in _text(provenance).splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key not in ("board", "nuttx"):
                continue
            if key in fields:
                raise ValueError(f"Duplicate {key} in provenance.txt")
            fields[key] = value
        if not fields.get("board") or not re.fullmatch("[0-9a-fA-F]{40}", fields.get("nuttx", "")):
            raise ValueError("Invalid board or NuttX revision in provenance.txt")
        return {"board": fields["board"], "nuttx_revision": fields["nuttx"].lower(),
                "config": _configuration(config), "config_path": config_path,
                "receipt_path": hashes, "elf_sha256": identity}
    build_path = root / "build.json"
    if not build_path.exists():
        return None
    build = _json(build_path)
    if build.get("workload") not in ("native NuttX memory_timing qualification fixture",
                                      "native isolated heap replay qualification"):
        return None
    if build.get("schema_version") != 1 or str(build.get("elf_sha256", "")).lower() != identity:
        raise ValueError("fixture build.json belongs to a different ELF (SHA-256 mismatch)")
    config = _text(config_path)
    if str(build.get("config_sha256", "")).lower() != _sha(config.encode("utf-8")):
        raise ValueError("resolved.config does not match fixture build.json")
    return {"board": "mps2-an521:nsh", "nuttx_revision": build["nuttx_revision"].lower(),
            "config": _configuration(config), "config_path": config_path,
            "receipt_path": build_path, "elf_sha256": identity}


def build_context(elf, identity=None):
    elf = Path(elf).absolute()
    if identity is None:
        metadata = elf.lstat()
        limit = 512 * MIB
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
            raise ValueError("ELF must be a regular file containing 1..512 MiB")
        digest, size = hashlib.sha256(), 0
        with elf.open("rb") as stream:
            for block in iter(lambda: stream.read(MIB), b""):
                size += len(block)
                if size > limit:
                    raise ValueError("ELF grew beyond 512 MiB")
                digest.update(block)
        if size != metadata.st_size or elf.stat().st_mtime_ns != metadata.st_mtime_ns:
            raise ValueError("ELF changed while hashing")
        identity = digest.hexdigest()
    roots = [elf.parent]
    if elf.parent.name == "nuttx":
        roots.append(elf.parent.parent)
    contexts = [context for root in roots if (context := _context_at(root, elf, identity))]
    if len(contexts) > 1:
        raise ValueError("Multiple build receipts match this ELF; supply an explicit layout")
    return contexts[0] if contexts else None


def _layout_matches(path, identity, require_identity):
    layout = _json(path)
    if layout.get("schema_version") != 1 or not isinstance(layout.get("regions"), list):
        raise ValueError("Layout requires schema_version 1 and regions")
    if "build" in layout:
        build = layout["build"]
        if not isinstance(build, dict) or str(build.get("elf_sha256", "")).lower() != identity:
            raise ValueError("Layout belongs to a different ELF (SHA-256 mismatch)")
    elif require_identity:
        raise ValueError("Adjacent layout has no build.elf_sha256; select it explicitly to apply a portable descriptor")


def _matches(entry, context):
    return entry["nuttx_board"] == context["board"] and entry["nuttx_revision"] == context["nuttx_revision"] and all(
        context["config"].get(key) == value for key, value in entry["required_config"].items())


def _specialize(selected, context):
    if context is None:
        return None
    layout = _json(ROOT / "layouts" / selected["layout"])
    layout["build"] = {"elf_sha256": context["elf_sha256"]}
    for rule in layout.get("configured_resources", []):
        if rule.get("enabled_by") and context["config"].get(rule["enabled_by"]) != "y":
            continue
        if "size_key" in rule:
            try:
                raw = context["config"].get(rule["size_key"], "").strip('"')
                size = int(raw, 16 if raw.lower().startswith("0x") else 10)
            except ValueError:
                size = None
            if size not in rule.get("allowed_sizes", []):
                size = None
        else:
            choices = [size for key, size in rule.get("size_choices", {}).items() if context["config"].get(key) == "y"]
            size = choices[0] if len(choices) == 1 else None
        if size:
            if any(resource["id"] == rule["id"] for resource in layout["physical_resources"]):
                raise ValueError("Duplicate configured resource ID")
            layout["physical_resources"].append({"id": rule["id"], "name": rule["name"], "size": size})
            layout["warnings"].append(f"{rule['name']}: {size} bytes are declared by the identity-matched effective build configuration. No CPU-to-device mapping or measured capacity is inferred; the resource remains unmapped.")
        else:
            layout["warnings"].append(f"{rule['name']}: the effective build configuration does not establish one supported capacity; its capacity remains unknown.")
    return layout


def discover(elf, identity, *, map_path=None, layout=None, board=None):
    """Return paths and provenance; never fetch the network or alter inputs."""
    elf = Path(elf)
    if board and layout:
        raise ValueError("Choose either --board or --layout")
    result = {"map": Path(map_path) if map_path else None, "automatic_map": not bool(map_path),
              "layout": None, "layout_data": None, "automatic_layout": not bool(layout or board), "warnings": []}
    if not map_path:
        candidates = list(dict.fromkeys(path for path in (elf.with_suffix(".map"), Path(str(elf) + ".map")) if path.exists()))
        if len(candidates) == 1:
            try:
                _text(candidates[0], 64 * MIB)
                result["map"] = candidates[0]
                result["warnings"].append(f"Automatically selected adjacent linker map {candidates[0].name}; the viewer checks its sections against the ELF.")
            except (OSError, ValueError) as error:
                result["warnings"].append(f"Skipped automatic linker map {candidates[0].name}: {error}. ELF placement remains available.")
        elif len(candidates) > 1:
            result["warnings"].append("Multiple adjacent linker maps were found; choose one explicitly.")
    if layout:
        _layout_matches(Path(layout), identity, False)
        result["layout"] = Path(layout)
        result["warnings"].append(f"Memory layout selected explicitly: {Path(layout).name}.")
        return result
    if board:
        selected = next((entry for entry in catalogue() if entry["id"] == board), None)
        if selected is None:
            raise ValueError(f"Unsupported board {board!r}; choose a bundled board or supply an explicit layout")
        result["layout"] = ROOT / "layouts" / selected["layout"]
        try:
            context = build_context(elf, identity)
        except (OSError, ValueError):
            context = None
        result["layout_data"] = _specialize(selected, context if context and _matches(selected, context) else None)
        result["warnings"].append(f"Explicit board selection: {selected['label']}. This applies the bundled reference topology, not runtime hardware verification.")
        return result
    candidates = list(dict.fromkeys(path for path in (elf.with_suffix(".layout.json"), elf.with_name("layout.json")) if path.exists()))
    matched = []
    for path in candidates:
        try:
            _layout_matches(path, identity, True)
            matched.append(path)
        except (OSError, ValueError) as error:
            result["warnings"].append(f"Skipped automatic layout {path.name}: {error}")
    if len(matched) == 1:
        result["layout"] = matched[0]
        result["warnings"].append(f"Automatically selected {matched[0].name} after verifying its ELF SHA-256.")
        return result
    if len(matched) > 1:
        result["warnings"].append("Multiple identity-matched adjacent layouts were found; choose one explicitly. No physical layout was applied.")
        return result
    try:
        context = build_context(elf, identity)
    except (OSError, ValueError) as error:
        result["warnings"].append(f"Automatic board layout was not applied: {error}. The overview uses linker regions or ELF placement.")
        return result
    if context:
        matches = [entry for entry in catalogue() if _matches(entry, context)]
        if len(matches) == 1:
            selected = matches[0]
            result["layout"] = ROOT / "layouts" / selected["layout"]
            result["layout_data"] = _specialize(selected, context)
            result["warnings"].append(f"Automatically selected {selected['label']} from an ELF/configuration-matched build receipt and pinned BSP revision. This is declared board topology, not measured runtime hardware.")
        else:
            result["warnings"].append(f"Build receipt identifies {context['board']} at NuttX {context['nuttx_revision']}, but no unique bundled layout matches its configuration. The overview uses linker regions or ELF placement.")
    else:
        result["warnings"].append("No identity-bound board metadata was found. The overview uses linker regions or ELF placement.")
    return result
