#!/usr/bin/env python3
"""Validate an application against one complete platform, not HAL feature flags."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_config import CAPABILITIES, detector_config, load, model_features, resolve, validate_target


def workspace_root(manifest: Path) -> Path:
    for directory in manifest.resolve().parents:
        candidate = directory / "Cargo.toml"
        if candidate.is_file() and "workspace" in load(candidate):
            return directory
    raise ValueError(f"no enclosing Cargo workspace for {manifest}")


def validate(app_manifest: Path, platform_name: str, target_os: str) -> dict:
    app_manifest = app_manifest.resolve()
    root = workspace_root(app_manifest)
    app = load(app_manifest)
    hal_root = root / "hal"
    for alias, dep in app.get("dependencies", {}).items():
        if not isinstance(dep, dict) or "path" not in dep:
            continue
        target = (app_manifest.parent / dep["path"]).resolve()
        if not target.is_relative_to(hal_root):
            continue
        relative = target.relative_to(hal_root)
        if len(relative.parts) != 1 or relative.name not in CAPABILITIES | {"common"}:
            raise ValueError(f"application dependency {alias} selects concrete HAL implementation {relative}")
        if set(dep.get("features", [])) & {"native", "mock", "nuttx", "std", "linux", "macos", "windows"}:
            raise ValueError(f"application dependency {alias} selects a provider feature; choose one platform")
    platform = resolve(platform_name, root)
    validate_target(platform, target_os)
    required = app.get("package", {}).get("metadata", {}).get("nxrs", {}).get("required-capabilities", [])
    if not isinstance(required, list) or any(cap not in CAPABILITIES for cap in required):
        raise ValueError("required-capabilities must list known HAL capabilities")
    for capability in required:
        if platform["hal"].get(capability, "none") == "none":
            raise ValueError(f"{platform_name}: missing required capability {capability}")
    return {
        "app_manifest": str(app_manifest),
        "app_package": app.get("package", {}).get("name"),
        "platform": platform_name,
        "target_os": target_os,
        "detector": detector_config(platform),
        "model_features": model_features(platform, app_manifest, root),
        "providers": [{"capability": cap, "implementation": impl, "package": f"nxrs-{cap}"}
                      for cap, impl in sorted(platform["hal"].items()) if impl != "none"],
        "profiles": [{"path": source, "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest()}
                     for source in platform["sources"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-manifest", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--target-os", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.app_manifest, args.platform, args.target_os)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Deployment: {error}\n")
    encoded = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
