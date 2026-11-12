"""Read-only binding of ordinary Rust captures to canonical NuttX build evidence."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.images import esp_idf_section_accounting, parse_sections
from rtos_harness.zephyr import resolved_config

BINS = ("ram-baseline", "ram-threads", "ram-std-inbox", "ram-crossbeam-select")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(root, relative):
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("target evidence escapes the build directory")
    return path


def verify_build(directory, readelf, binary):
    if binary not in BINS:
        raise ValueError("unrecognized ordinary Rust RAM binary")
    root = Path(directory).resolve(strict=True)
    hashes = {}
    for line in inside(root, "binaries.sha256").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] in hashes:
            raise ValueError("invalid/duplicate canonical build hash entry")
        hashes[match[2]] = match[1]
    configs = [name for name in hashes if name.endswith("/resolved.config")]
    if len(configs) != 1:
        raise ValueError("canonical build must pin one resolved.config")
    original_root = str(PurePosixPath(configs[0]).parent)
    selected = {}
    for name in ("nuttx/nuttx", "nuttx/nuttx.merged.bin", "resolved.config",
                 "provider-selection.json", "xtensa-esp32s3-nuttx.json"):
        path = inside(root, name)
        expected = hashes.get(original_root + "/" + name)
        if expected is None or sha256(path) != expected:
            raise ValueError(f"target artifact differs from canonical build hash: {name}")
        selected[name] = expected
    proof_path = inside(root, "provenance.txt")
    proof = proof_path.read_text()
    fields = {}
    for line in proof.splitlines():
        if re.match(r"^[a-z_]+=", line):
            key, value = line.split("=", 1)
            if key in fields and key not in ("target_c_source", "kconfig_enable", "kconfig_disable"):
                raise ValueError("duplicate canonical provenance identity")
            fields[key] = value
    required = {"app_package": "nxrs-ram-footprint-std", "app_bin": binary,
                "app_command": "ramprobe", "platform": "esp32s3-ram-footprint",
                "abi_profile": "minimal", "target": "xtensa-esp32s3-nuttx",
                "nuttx_build_mode": "flat", "nxrs_dirty": "no"}
    if any(fields.get(key) != value for key, value in required.items()):
        raise ValueError("ordinary Rust target build identity/policy mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", fields.get("nxrs", "")):
        raise ValueError("canonical build source revision is missing")
    policy_keys = ("size_optimized", "trace_only_backtrace", "panic_immediate_abort")
    if any(fields.get(key) not in ("0", "1") for key in policy_keys):
        raise ValueError("canonical build optimization policy is missing or invalid")
    values = resolved_config(inside(root, "resolved.config").read_text())
    if (values.get("CONFIG_BUILD_FLAT") != "y" or values.get("CONFIG_STACK_COLORATION") != "y" or
            values.get("CONFIG_SMP", "n") != "n" or values.get("CONFIG_ESP32S3_SPIRAM", "n") != "n"):
        raise ValueError("target configuration lacks flat/single-core/internal-RAM stack observations")
    header = inside(root, "nuttx/include/nuttx/config.h")
    context = json.loads(inside(root, "native-context.json").read_text())
    if (context.get("schema") != 1 or context.get("config_sha256") != sha256(header) or
            context.get("target") != required["target"]):
        raise ValueError("native observation ABI used a different target configuration")
    # The canonical context hashes generated config.h, not resolved.config.
    # Check the observation controls in both representations; retain the header
    # hash without pretending that this regenerates or verifies all C headers.
    defines = dict(re.findall(r"(?m)^#define\s+(CONFIG_\w+)\s+([^\n]+)$", header.read_text()))
    if (any(defines.get(key) != "1" for key in ("CONFIG_BUILD_FLAT", "CONFIG_STACK_COLORATION")) or
            any(key in defines for key in ("CONFIG_SMP", "CONFIG_ESP32S3_SPIRAM"))):
        raise ValueError("generated native configuration disagrees with the RAM observation controls")
    if not re.fullmatch(r"[0-9a-f]{64}", context.get("headers_sha256", "")):
        raise ValueError("native header snapshot identity is missing")
    sections = parse_sections(subprocess.check_output(
        [str(readelf), "-W", "-S", str(inside(root, "nuttx/nuttx"))], text=True))
    if any("A" in row["flags"] and row["size"] and row["address"] < 0x3E000000
           and row["address"] + row["size"] > 0x3D000000 for row in sections):
        raise ValueError("target ELF contains external RAM")
    sources = {}
    for relative in ("tests/ram-footprint-std/Cargo.toml", "tests/ram-footprint-std/ffi/observe.c"):
        matches = [digest for path, digest in hashes.items() if path.endswith("/" + relative)]
        if len(matches) != 1:
            raise ValueError("canonical build lacks fixture manifest/observer identity")
        sources[relative] = matches[0]
    return {"source_commit": fields["nxrs"], "binary": binary, "command": "ramprobe",
            "profile": required["platform"], "artifact_sha256": selected,
            "provenance_sha256": sha256(proof_path),
            "native_context_sha256": sha256(inside(root, "native-context.json")),
            "native_config_header_sha256": sha256(header),
            "native_headers_snapshot_sha256": context["headers_sha256"],
            "build_hash_receipt_sha256": sha256(inside(root, "binaries.sha256")),
            "source_sha256": sources, "section_accounting": esp_idf_section_accounting(sections),
            "optimization_policy": {key: fields[key] for key in policy_keys},
            "stack_sizes_are_observations_not_proven_bounds": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--readelf", required=True)
    parser.add_argument("--binary", choices=BINS, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = {"schema": 1, "kind": "ordinary-rust-ram-preflight",
              "device_io_performed": False, "physical_hardware_qualified": False,
              "build_identity": verify_build(args.build, args.readelf, args.binary)}
    with args.out.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(f"ORDINARY_RUST_RAM_PREFLIGHT_PASS binary={args.binary}")


if __name__ == "__main__":
    main()
