#!/usr/bin/env python3
"""Resolve one checked-in product platform; providers are private build details."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"camera", "storage", "transport", "led", "imu", "gnss", "wifi", "ble", "update"}
HOST_OS = {"linux", "macos", "windows"}


def load(path: Path) -> dict:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def profile_path(name: str, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"invalid platform name: {name!r}")
    matches = [directory / f"{name}.toml" for directory in (
        root / "platform/profiles", root / "platform/nuttx/platforms")
        if (directory / f"{name}.toml").is_file()]
    if len(matches) != 1:
        raise ValueError(f"unknown or ambiguous platform: {name}")
    return matches[0].resolve()


def resolve(name: str, root: Path = ROOT, chain: tuple[str, ...] = ()) -> dict:
    if name in chain or len(chain) >= 16:
        raise ValueError(f"platform inheritance cycle/depth: {' -> '.join((*chain, name))}")
    path = profile_path(name, root)
    child = load(path)
    if "hal-features" in child:
        raise ValueError(f"{name}: retired hal-features; platform must own [hal] bindings")
    parent = child.pop("extends", None)
    if parent is not None and not isinstance(parent, str):
        raise ValueError(f"{name}: extends must name one platform")
    data = resolve(parent, root, (*chain, name)) if parent else {"hal": {}, "sources": []}
    bindings = child.pop("hal", {})
    if not isinstance(bindings, dict):
        raise ValueError(f"{name}: [hal] must be a table")
    bindings = {**data.get("hal", {}), **bindings}
    data = {**data, **child, "hal": bindings, "name": name,
            "sources": [*data["sources"], str(path)]}
    # Inheritance merges capability bindings; other fields/tables replace the
    # parent's value. In particular, Kconfig is not concatenated accidentally.
    for capability, provider in bindings.items():
        if capability not in CAPABILITIES or not isinstance(provider, str):
            raise ValueError(f"{name}: unknown capability or invalid binding: {capability}")
        manifest = root / "hal" / capability / "Cargo.toml"
        declared = load(manifest).get("package", {}).get("metadata", {}).get("nxrs", {}).get("platforms", [])
        if provider != "none" and provider not in declared:
            raise ValueError(f"{name}: unsupported {capability} implementation: {provider}")
    return data


def validate_target(platform: dict, target_os: str) -> None:
    supported = platform.get("target-os")
    if supported is not None:
        if (not isinstance(supported, list) or not supported
                or any(not isinstance(value, str) or value not in HOST_OS | {"nuttx", "none"}
                       for value in supported)):
            raise ValueError(f"{platform['name']}: target-os must be a nonempty array of supported OS names")
        if target_os not in supported:
            raise ValueError(f"{platform['name']}: platform does not support target OS {target_os}")
    execution = platform.get("execution", "nuttx" if "board" in platform else "any")
    if execution not in {"any", "std", "nuttx", "core"}:
        raise ValueError(f"{platform['name']}: unknown execution environment: {execution}")
    if ((execution == "std" and target_os not in HOST_OS)
            or (execution == "nuttx" and target_os != "nuttx")
            or (execution == "core" and target_os != "none")):
        raise ValueError(f"{platform['name']}: execution {execution} does not support target OS {target_os}")
    for capability, provider in platform["hal"].items():
        allowed = (provider in {"none", "mock"}
                   or (provider == "std" and target_os in HOST_OS)
                   or (provider in {"linux", "macos"} and provider == target_os)
                   or (provider == "nuttx" and target_os == "nuttx")
                   or (provider == "nuttx" and target_os == "none" and capability in {"camera", "storage"}))
        if not allowed:
            raise ValueError(f"{platform['name']}: {capability}/{provider} does not support target OS {target_os}")


def cargo_output(platform: dict, capability: str, target_os: str, root: Path = ROOT) -> list[str]:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown HAL capability: {capability}")
    validate_target(platform, target_os)
    manifests = {root / "hal" / name / "Cargo.toml" for name in platform["hal"]}
    manifests.add(root / "hal" / capability / "Cargo.toml")
    return [
        *(f"cargo:rerun-if-changed={source}" for source in platform["sources"]),
        *(f"cargo:rerun-if-changed={manifest}" for manifest in sorted(manifests)),
        f'cargo:rustc-cfg=nxrs_hal_platform="{platform["hal"].get(capability, "none")}"',
    ]


def update_layout(platform: dict) -> dict:
    boot = platform.get("boot", {}).get("layout")
    fixture = platform.get("update", {})
    if boot is not None:
        if fixture:
            raise ValueError("update resource cannot override a boot policy layout")
        layout = boot
    else:
        if fixture.get("scope") != "qualification-only" or set(fixture) != {"scope", "layout"}:
            raise ValueError("non-boot update resource needs an explicit qualification layout")
        layout = fixture["layout"]
    keys = {"flash_bytes", "primary_offset", "secondary_offset", "slot_bytes", "scratch_offset", "scratch_bytes",
            "erase_bytes", "header_bytes", "write_align", "max_align", "max_sectors"}
    if not isinstance(layout, dict) or set(layout) != keys or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in layout.values()):
        raise ValueError("invalid update resource layout")
    return layout


def emit_update_layout(platform: dict, output: Path) -> str:
    layout = update_layout(platform)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "update-resource-layout.rs"
    source = "const RESOURCE_LAYOUT: crate::Layout = crate::Layout {" + ",".join(f"{key}:{value}" for key,value in sorted(layout.items())) + "};\n"
    destination.write_text(source)
    return "cargo:rustc-env=NXRS_UPDATE_LAYOUT_RS=" + str(destination)


def native_helpers(platform: dict, root: Path = ROOT) -> list[Path]:
    """C ABI shims belong to the selected platform, not application features."""
    capabilities = {name for name, provider in platform["hal"].items() if provider == "nuttx"}
    helpers = {path for name in capabilities
               for path in (root / "hal" / name / "src/platform/nuttx/ffi").glob("*.c")}
    return sorted(helpers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--capability", choices=sorted(CAPABILITIES))
    parser.add_argument("--target-os")
    parser.add_argument("--emit-cargo", action="store_true")
    parser.add_argument("--native-out", type=Path)
    parser.add_argument("--target-triple")
    args = parser.parse_args()
    try:
        platform = resolve(args.platform)
        if args.emit_cargo:
            if not args.capability or not args.target_os:
                raise ValueError("--emit-cargo requires --capability and --target-os")
            output = cargo_output(platform, args.capability, args.target_os)
            if args.capability == "update" and platform["hal"].get("update") == "nuttx":
                if args.native_out is None:
                    raise ValueError("update resource configuration needs a Cargo output directory")
                output.append(emit_update_layout(platform, args.native_out))
            if args.native_out is not None and platform["hal"].get(args.capability) in {"linux", "macos"}:
                if not args.target_triple:
                    raise ValueError("native compilation requires --target-triple")
                from hal_native import build
                output += build(ROOT / "hal" / args.capability, args.target_os,
                                args.target_triple, args.native_out)
            print("\n".join(output))
        else:
            if args.target_os:
                validate_target(platform, args.target_os)
            print(json.dumps(platform, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        parser.exit(1, f"Platform configuration: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
