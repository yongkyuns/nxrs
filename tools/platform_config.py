#!/usr/bin/env python3
"""Resolve one checked-in product platform; providers are private build details."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"console", "execution", "detector", "camera", "config", "storage", "transport", "led", "imu", "gnss", "wifi", "ble", "tls", "boot", "watchdog"}
HOST_OS = {"linux", "macos", "windows"}
MODEL_FEATURES = {"vww": "person-model", "pico": "pico-model"}


def load(path: Path) -> dict:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def profile_path(name: str, root: Path = ROOT) -> Path:
    if name in {"std", "native"}:
        raise ValueError(f"unknown or ambiguous platform: {name}; select an actual OS or product profile")
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
        if provider in {"std", "native"} or (provider != "none" and provider not in declared):
            raise ValueError(f"{name}: unsupported {capability} implementation: {provider}")
    detector_config(data)
    scheduling_config(data)
    console_config(data)
    network_initialization(data)
    gnss_config(data)
    imu_config(data)
    return data


def gnss_config(platform: dict) -> dict | None:
    config = platform.get("gnss", {})
    if platform["hal"].get("gnss") != "nuttx":
        if config:
            raise ValueError("GNSS device configuration requires its NuttX binding")
        return None
    if not isinstance(config, dict) or set(config) != {"device", "baud"}:
        raise ValueError("GNSS requires a UART device and baud")
    device = config["device"]
    if (not isinstance(device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9_/.-]{1,120}", device)
            or ".." in device.split("/") or type(config["baud"]) is not int
            or config["baud"] not in {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}):
        raise ValueError("invalid GNSS UART binding")
    return config


def imu_config(platform: dict) -> dict | None:
    config = platform.get("imu", {})
    if platform["hal"].get("imu") != "nuttx":
        if config:
            raise ValueError("IMU device configuration requires its NuttX binding")
        return None
    if not isinstance(config, dict) or set(config) != {"driver", "device", "address", "frequency"}:
        raise ValueError("IMU requires driver, I2C device, address and frequency")
    device = config["device"]
    if (config["driver"] != "bmi270" or not isinstance(device, str)
            or not re.fullmatch(r"/dev/[A-Za-z0-9_/.-]{1,120}", device)
            or ".." in device.split("/") or type(config["address"]) is not int
            or config["address"] not in {0x68, 0x69}
            or type(config["frequency"]) is not int or config["frequency"] not in {100000, 400000}):
        raise ValueError("invalid BMI270 I2C binding")
    return config


def detector_config(platform: dict) -> dict | None:
    """Product-owned model and execution budget; an empty table disables detection."""
    config = platform.get("detector", {})
    if not isinstance(config, dict):
        raise ValueError(f"{platform['name']}: [detector] must be a table")
    if not config:
        return None
    if platform["hal"].get("detector", "none") == "none":
        raise ValueError(f"{platform['name']}: detector configuration requires a detector HAL binding")
    if set(config) != {"model", "stack-bytes", "sample-ms"}:
        raise ValueError(f"{platform['name']}: detector requires model, stack-bytes and sample-ms")
    if not isinstance(config["model"], str) or config["model"] not in MODEL_FEATURES:
        raise ValueError(f"{platform['name']}: unknown detector model: {config['model']}")
    for key in ("stack-bytes", "sample-ms"):
        if type(config[key]) is not int or not 0 < config[key] <= 0x7fffffff:
            raise ValueError(f"{platform['name']}: detector {key} must be a positive 32-bit integer")
    return config


def scheduling_config(platform: dict) -> dict | None:
    config = platform.get("scheduling", {})
    if platform["hal"].get("execution", "none") == "none":
        if config:
            raise ValueError("scheduling requires an execution HAL binding")
        return None
    if (not isinstance(config, dict) or set(config) != {"background", "normal"}
            or any(type(value) is not int or not 1 <= value <= 255 for value in config.values())
            or config["background"] >= config["normal"]):
        raise ValueError("scheduling requires ordered bounded background and normal priorities")
    return config


def console_config(platform: dict) -> str | None:
    config = platform.get("console", {})
    if platform["hal"].get("console", "none") == "none":
        if config:
            raise ValueError("console configuration requires a console HAL binding")
        return None
    if not isinstance(config, dict) or set(config) != {"device"}:
        raise ValueError("console requires one device")
    device = config["device"]
    if (not isinstance(device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9_/.-]{1,120}", device)
            or ".." in device.split("/")):
        raise ValueError("invalid console device path")
    return device


def network_initialization(platform: dict) -> str:
    init = platform.get("init", {})
    if not isinstance(init, dict) or init.get("network", "external") not in ("external", "on-acquire"):
        raise ValueError("invalid network initialization policy")
    return init.get("network", "external")


def uses_detector(manifest: Path, root: Path = ROOT, seen: set[Path] | None = None) -> bool:
    """Follow ordinary local production dependencies, never dev-only qualification edges."""
    manifest = manifest.resolve()
    seen = set() if seen is None else seen
    if manifest in seen:
        return False
    seen.add(manifest)
    if manifest == (root / "hal/detector/Cargo.toml").resolve():
        return True
    data = load(manifest)
    tables = [data, *data.get("target", {}).values()]
    workspace = load(root / "Cargo.toml").get("workspace", {}).get("dependencies", {})
    for table in tables:
        for alias, dependency in table.get("dependencies", {}).items():
            if not isinstance(dependency, dict):
                continue
            base = manifest.parent
            if dependency.get("workspace"):
                inherited = workspace.get(alias, {})
                dependency = {**(inherited if isinstance(inherited, dict) else {}), **dependency}
                base = root
            if "path" in dependency and not dependency.get("optional", False):
                if uses_detector(base / dependency["path"] / "Cargo.toml", root, seen):
                    return True
    return False


def model_features(platform: dict, manifest: Path, root: Path = ROOT) -> list[str]:
    config = detector_config(platform)
    if config and uses_detector(manifest, root):
        return ["nxrs-detector/" + MODEL_FEATURES[config["model"]]]
    return []


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
                   or (provider in HOST_OS and provider == target_os)
                   or (provider == "nuttx" and target_os == "nuttx"))
        if not allowed:
            raise ValueError(f"{platform['name']}: {capability}/{provider} does not support target OS {target_os}")


def cargo_output(platform: dict, capability: str, target_os: str, root: Path = ROOT) -> list[str]:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown HAL capability: {capability}")
    validate_target(platform, target_os)
    manifests = {root / "hal" / name / "Cargo.toml" for name in platform["hal"]}
    manifests.add(root / "hal" / capability / "Cargo.toml")
    resources = []
    if capability == "gnss" and platform["hal"].get(capability) == "nuttx":
        config = gnss_config(platform)
        resources.extend(["cargo:rustc-env=GNSS_DEVICE=" + config["device"],
                          "cargo:rustc-env=GNSS_BAUD=" + str(config["baud"])])
    if capability == "imu" and platform["hal"].get(capability) == "nuttx":
        config = imu_config(platform)
        resources.extend("cargo:rustc-env=IMU_" + key.upper() + "=" + str(config[key])
                         for key in ("device", "address", "frequency"))
    if capability == "wifi" and platform["hal"].get(capability) == "nuttx":
        resources.append("cargo:rustc-env=WIFI_INITIALIZATION=" + network_initialization(platform))
    if capability == "console" and platform["hal"].get(capability, "none") != "none":
        resources.append("cargo:rustc-env=CONSOLE_DEVICE=" + console_config(platform))
    if capability == "execution" and platform["hal"].get(capability, "none") != "none":
        resources.extend("cargo:rustc-env=EXECUTION_" + key.upper() + "_PRIORITY=" + str(value)
                         for key, value in scheduling_config(platform).items())
    return [
        *resources,
        *(f"cargo:rerun-if-changed={source}" for source in platform["sources"]),
        *(f"cargo:rerun-if-changed={manifest}" for manifest in sorted(manifests)),
        *(["cargo:rustc-cfg=hal_mock"] if platform["hal"].get(capability) == "mock"
          else ["cargo:rustc-cfg=hal_enabled"]
          if platform["hal"].get(capability, "none") != "none" else []),
    ]


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
    parser.add_argument("--detector-config", action="store_true")
    parser.add_argument("--native-out", type=Path)
    parser.add_argument("--target-triple")
    args = parser.parse_args()
    try:
        platform = resolve(args.platform)
        if args.detector_config:
            # Private build-script protocol, with all inherited inputs tracked.
            for source in platform["sources"]:
                print(f"cargo:rerun-if-changed={source}")
            config = detector_config(platform)
            if config:
                print(config["model"], config["stack-bytes"], config["sample-ms"], sep="\n")
        elif args.emit_cargo:
            if not args.capability or not args.target_os:
                raise ValueError("--emit-cargo requires --capability and --target-os")
            output = cargo_output(platform, args.capability, args.target_os)
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
