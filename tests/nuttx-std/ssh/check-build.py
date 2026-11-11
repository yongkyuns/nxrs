#!/usr/bin/env python3
"""Check effective image evidence; never substitute this for device tests."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]


def check(out, enabled):
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
        for name in ("NETUTILS_DROPBEAR", "NET_TCP", "PSEUDOTERM", "PIPES", "SCHED_WAITPID",
                     "SCHED_HAVE_PARENT", "SCHED_CHILD_STATUS", "FSUTILS_PASSWD",
                     "DEV_RANDOM", "DEV_URANDOM_ARCH", "CRYPTO_CRYPTODEV",
                     "CRYPTO_CRYPTODEV_SOFTWARE_CRYPTO", "CRYPTO_SW_AES", "TTY_SIGINT"):
            require(name)
        for name in ("NETUTILS_DROPBEAR_SCP", "NETUTILS_DROPBEAR_COMPRESSION",
                     "NETUTILS_DROPBEAR_GENERATE_HOSTKEY"):
            disabled(name)
        if not re.search(r"\b[TtWw]\s+dropbear_main$", symbols, re.M):
            raise ValueError("native Dropbear entry absent from final symbols")
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
    (out / "ssh-build-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]), sys.argv[2] == "1")
    except (ValueError, OSError) as error:
        raise SystemExit(f"SSH build evidence failed: {error}")
