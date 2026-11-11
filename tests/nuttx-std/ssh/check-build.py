#!/usr/bin/env python3
"""Check effective image evidence; never substitute this for device tests."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
DROPBEAR_SOURCE_RECEIPT = ROOT / "upstream/nuttx-apps/dropbear-source-receipt.json"


def check_dropbear_sources(out, configured_revision):
    """Match real prepared sources to the reviewed full nested patch sequence.

    This check can run independently as a host control. It neither creates a
    cross-build receipt nor claims that these sources have executed on a device.
    """
    expected_bytes = DROPBEAR_SOURCE_RECEIPT.read_bytes()
    expected = json.loads(expected_bytes)
    if (expected.get("schema_version") != 1
            or expected.get("repository") != "mkj/dropbear"):
        raise ValueError("unsupported Dropbear source receipt")
    if configured_revision != expected.get("revision"):
        raise ValueError("prepared Dropbear revision differs from source receipt")
    port = out / "apps/netutils/dropbear"

    def entries(key, prefix):
        values = expected.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f"Dropbear source receipt has no {key}")
        paths = []
        for value in values:
            name = value.get("path", "") if isinstance(value, dict) else ""
            path = PurePosixPath(name)
            if (not name.startswith(prefix + "/") or path.is_absolute()
                    or ".." in path.parts or str(path) != name or name in paths):
                raise ValueError(f"invalid or duplicate Dropbear receipt path: {name}")
            paths.append(name)
        return values, paths

    patches, patch_order = entries("nested_patches", "patch")
    files, source_paths = entries("files", "src")
    touched = set()
    for entry in patches:
        path = port / entry["path"]
        if not path.is_file():
            raise ValueError(f"prepared Dropbear patch missing: {entry['path']}")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
            raise ValueError(f"prepared Dropbear patch differs: {entry['path']}")
        touched.update(re.findall(r"^\+\+\+ b/(\S+)", content.decode(), re.M))
    if touched != set(source_paths):
        raise ValueError("Dropbear source receipt does not cover the complete nested patch sequence")

    recipe_hashes = {}
    for name in ("Makefile", "CMakeLists.txt"):
        path = port / name
        if not path.is_file():
            raise ValueError(f"prepared Dropbear recipe missing: {name}")
        content = path.read_bytes()
        active_lines = "\n".join(line for line in content.decode().splitlines()
                                 if not line.lstrip().startswith("#"))
        declared = ["patch/" + name for name in re.findall(
            r"\bpatch(?:/|\$\(DELIM\))([A-Za-z0-9_.-]+\.patch)", active_lines)]
        if declared != patch_order:
            raise ValueError(f"prepared Dropbear nested patch sequence differs: {name}")
        recipe_hashes[name] = hashlib.sha256(content).hexdigest()

    for entry in files:
        path = port / "dropbear" / entry["path"]
        if not path.is_file():
            raise ValueError(f"prepared Dropbear source missing: {entry['path']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("prepared_sha256"):
            raise ValueError(f"prepared Dropbear source differs: {entry['path']}")

    return {
        "schema_version": 1, "evidence_class": "prepared_source_identity",
        "expected_receipt_path": str(DROPBEAR_SOURCE_RECEIPT.relative_to(ROOT)),
        "expected_receipt_sha256": hashlib.sha256(expected_bytes).hexdigest(),
        "expected_receipt": expected, "recipe_sha256": recipe_hashes,
        "verification_scope": "prepared patch bytes, declared sequence and all affected source bytes",
        "device_qualification": "pending",
    }


def check(out, enabled):
    # A failed recheck must not leave a previous successful receipt beside a
    # changed image or changed prepared source tree.
    for name in ("ssh-build-evidence.json", "ssh-dropbear-source-receipt.json",
                 "ssh-localoptions.h"):
        (out / name).unlink(missing_ok=True)
    required = ("resolved.config", "symbols.txt", "provenance.txt", "std-patch.json",
                "std-main-thread-once.patch", "nuttx/nuttx",
                "nuttx/nuttx.bin", "nuttx/nuttx.merged.bin")
    for name in required:
        path = out / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"required cross-build artifact missing or empty: {name}")
    std = json.loads((out / "std-patch.json").read_text())
    patch_hash = hashlib.sha256((out / "std-main-thread-once.patch").read_bytes()).hexdigest()
    if (std.get("main_thread_initialization") != "nuttx-image-once"
            or std.get("patchset_sha256", {}).get("std-main-thread-once.patch") != patch_hash):
        raise ValueError("prepared Rust main-thread initialization fix is missing or inconsistent")
    config_path = out / "resolved.config"
    config = dict(re.findall(r"^(CONFIG_\w+)=(.*)$", config_path.read_text(), re.M))
    symbols_path = out / "symbols.txt"
    symbols = symbols_path.read_text()
    def require(name, value="y"):
        if config.get("CONFIG_" + name) != value:
            raise ValueError(f"effective {name} must be {value}")
    def disabled(name):
        if config.get("CONFIG_" + name) not in (None, "n"):
            raise ValueError(f"effective {name} must be disabled")
    require("NSH_DISABLEBG")
    require("BUILD_FLAT")
    require("TLS_GLOBAL_KEYS")
    disabled("SMP")
    disabled("NSH_PIPELINE")
    disabled("NSH_DROPBEAR")
    if enabled:
        for name in ("NETUTILS_DROPBEAR", "NET_TCP", "PSEUDOTERM", "PIPES", "ARCH_SETJMP_H", "SCHED_WAITPID",
                     "SCHED_HAVE_PARENT", "SCHED_CHILD_STATUS", "FSUTILS_PASSWD",
                     "DEV_RANDOM", "DEV_URANDOM_ARCH", "CRYPTO_CRYPTODEV",
                     "CRYPTO_CRYPTODEV_SOFTWARE_CRYPTO", "CRYPTO_SW_AES", "TTY_SIGINT"):
            require(name)
        for name in ("NETUTILS_DROPBEAR_SCP", "NETUTILS_DROPBEAR_COMPRESSION",
                     "NETUTILS_DROPBEAR_GENERATE_HOSTKEY"):
            disabled(name)
        if not re.search(r"\b[TtWw]\s+dropbear_main$", symbols, re.M):
            raise ValueError("native Dropbear entry absent from final symbols")
        prepared_sources = check_dropbear_sources(
            out, config.get("CONFIG_NETUTILS_DROPBEAR_COMMIT", "").strip('"'))
    else:
        for name in ("NETUTILS_DROPBEAR", "FSUTILS_PASSWD", "CRYPTO_CRYPTODEV",
                     "CRYPTO_CRYPTODEV_SOFTWARE_CRYPTO"):
            disabled(name)
        if re.search(r"\b(?:dropbear_main|dropbear_nshsession_cleanup|svr_session|passwd_verify)$", symbols, re.M):
            raise ValueError("SSH/password implementation leaked into disabled image")
    identities = {}
    for name in required:
        path = out / name
        if path.is_file(): identities[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = {
        "schema": 1, "evidence_class": "crossbuild", "ssh_enabled": enabled,
        "device_qualification": "pending", "identities_sha256": identities,
        "rust_main_thread_initialization": std["main_thread_initialization"],
        "effective_ssh_config": {key: value for key, value in config.items()
                                 if "DROPBEAR" in key or key.startswith("CONFIG_FSUTILS_PASSWD")},
        "algorithms_source": "apps/netutils/dropbear/port/localoptions.h",
        "exclusion_scope": "effective configuration and final symbols; fresh-build fetch audit is separate",
    }
    port = out / "apps/netutils/dropbear/port/localoptions.h"
    if enabled:
        if not port.is_file() or port.stat().st_size == 0:
            raise ValueError("prepared native algorithm configuration is missing")
        content = port.read_bytes()
        (out / "ssh-localoptions.h").write_bytes(content)
        evidence["algorithm_configuration_sha256"] = hashlib.sha256(content).hexdigest()
        receipt_bytes = (json.dumps(prepared_sources, indent=2) + "\n").encode()
        (out / "ssh-dropbear-source-receipt.json").write_bytes(receipt_bytes)
        evidence["dropbear_prepared_source_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
    (out / "ssh-build-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]), sys.argv[2] == "1")
    except (ValueError, OSError) as error:
        raise SystemExit(f"SSH build evidence failed: {error}")
