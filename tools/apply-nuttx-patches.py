#!/usr/bin/env python3
"""Apply tracked upstream patch series to isolated dependency build copies only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_MANIFEST = ROOT / "upstream/rust-llvm/proposals/series.json"
SERIES = {
    "nuttx": (
        "0001-Fix-ESP32S3-BLE-advertising.patch",
        "0002-Add-ESP32S3-NimBLE-HCI-configuration-option.patch",
        "0003-Add-ESP32-S3-camera-driver-for-OV3660-sensor.patch",
        "0004-Enable-ESP32-S3-Wi-Fi-HAL-helpers-under-NuttX.patch",
        "0005-net-udp-return-zero-length-datagrams-from-readahead.patch",
        "0006-flat-build-global-pthread-keys.patch",
        "0007-esp32s3-spiram-size-mismatch-return.patch",
        "0008-esp32s3-devkit-freenove-userled.patch",
        "0009-bcmf-accept-disabled-encoding.patch",
        "0011-pico2w-wifi-board.patch",
        "0012-rp23xx-cyw43439-backport-compatibility.patch",
        "0013-bcmf-gspi-release-receive-frames.patch",
        "0014-cyw43439-shared-bus-bluetooth.patch",
        "0015-rp23xx-cyw43439-irq-state.patch",
        "0016-cdcacm-1200bps-bootloader.patch",
        "0017-bluetooth-hci-sync-lifetime.patch",
        "0018-esp32s3-ble-pair-kernel-allocators.patch",
        "0019-bluetooth-host-receive-capacity.patch",
        "0020-bluetooth-att-mtu-negotiation.patch",
        "0021-rp2xxx-cyw43439-clm-alignment.patch",
        "0022-net-tcp-poll-connecting-sockets.patch",
        "0023-bluetooth-advertising-errors.patch",
        "0024-wifi-statistics-capability.patch",
        "0025-bcmf-frame-boundary-observer.patch",
        "0026-bluetooth-acl-send-log-level.patch",
        "0027-bcmf-gspi-frame-capacity.patch",
        "0028-rp23xx-cyw43439-board-calibration.patch",
        "0029-bcmf-gspi-tx-backpressure.patch",
        "0030-bt-sync-legacy-advertising-state.patch",
        "0031-bcmf-association-event-diagnostics.patch",
        "0032-bcmf-receive-diagnostics.patch",
        "0033-bcmf-phy-counters.patch",
        "0034-bcmf-daemon-diagnostics.patch",
        "0035-bluetooth-gatt-connection-callbacks.patch",
        "0036-bcmf-gspi-validate-receive-frames.patch",
        "0037-bcmf-gspi-stage-byte-transfers.patch",
        "0038-bcmf-gspi-propagate-register-errors.patch",
        "0039-bluetooth-reclaim-connection-credits.patch",
        "0040-bcmf-gspi-align-f2-transfers.patch",
        "0041-bcmf-gspi-acknowledge-error-interrupts.patch",
        "0042-tcp-preserve-retransmission-progress.patch",
        "0043-bcmf-hci-protocol-diagnostics.patch",
        "0044-rp23xx-cyw43439-pio-rx-count.patch",
        "0045-rp23xx-dma-preserve-pending-interrupts.patch",
        "0046-bcmf-hci-bus-fault-readback.patch",
        "0047-rp23xx-cyw43439-dma-lifetime.patch",
        "0048-rp23xx-cyw43439-pio-rx-fifo.patch",
        "0049-rp23xx-pio-dreq-index.patch",
        "0050-bcmf-hci-ram-readback.patch",
        "0051-bluetooth-retain-disconnect-reason.patch",
        "0052-esp32s3-ble-wrap-kernel-deallocator.patch",
        "0053-rp23xx-cyw43439-read-observer.patch",
        "0054-bluetooth-link-event-diagnostics.patch",
        "0055-rp23xx-cyw43439-write-observer.patch",
        "0056-bluetooth-le-connection-parameters.patch",
        "0057-bluetooth-setup-stage-diagnostics.patch",
        "0058-bluetooth-buffer-setup-diagnostics.patch",
        "0059-bluetooth-complete-pending-att-on-disconnect.patch",
        "0060-bluetooth-service-discovery-bounds.patch",
        "0061-bluetooth-negotiated-gatt-mtu.patch",
        "0062-bluetooth-discovery-completion-status.patch",
        "0063-bluetooth-expand-short-uuid-correctly.patch",
        "0064-rp23xx-picoboot-only-usb-recovery.patch",
    ),
    "nuttx-apps": (
        "0001-Add-ESP32S3-VHCI-transport-support-for-NimBLE.patch",
        "0002-nsh-dmesg-reset-options.patch",
    ),
    "esp-hal-3rdparty": (
        "0001-mbedtls-pair-configured-allocators.patch",
    ),
    "rust-llvm": (
        "0001-Xtensa-custom-lower-i32-rotates.patch",
        "0002-Xtensa-default-eligible-hardware-loops.patch",
        "0003-Xtensa-model-loop-end-fallthrough.patch",
        "0004-Xtensa-use-immediate-SAR-for-constant-funnels.patch",
        "0005-Xtensa-refresh-affected-codegen-checks.patch",
        "0006-Xtensa-model-ESP32S3-multiply-result-latency.patch",
    ),
}
MARKERS = {
    "nuttx": "libs/libc/tls/Kconfig",
    "nuttx-apps": "wireless/bluetooth/nimble/Makefile.nimble",
    "esp-hal-3rdparty": "components/mbedtls/port/esp_mem.c",
    "rust-llvm": "llvm/lib/Target/Xtensa/XtensaISelLowering.cpp",
}
PINNED_METADATA = {"rust-llvm": ROOT / "upstream/rust-llvm/upstream.json"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_env(source):
    # A build archive may live under the nxrs checkout. Prevent git apply from
    # treating its paths as relative to that unrelated outer repository.
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)}


def run_git_apply(source, *options, patch):
    return subprocess.run(
        ["git", "apply", *options, str(patch)],
        cwd=source, env=git_env(source), text=True, capture_output=True,
    )


def _manifest_path(value, *, test_path=False):
    """Validate and normalize a repository-relative LLVM manifest path."""
    if (not isinstance(value, str) or not value or "\\" in value or
            value.startswith("/") or re.match(r"^[A-Za-z]:", value)):
        raise ValueError(f"unsafe path in Rust LLVM proposal manifest: {value!r}")
    path = Path(value)
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or not path.parts:
        raise ValueError(f"unsafe path in Rust LLVM proposal manifest: {value!r}")
    if test_path and (len(parts) < 3 or parts[:2] != ["llvm", "test"]):
        raise ValueError(f"proposal test path must be under llvm/test: {value}")
    if not test_path and parts[0] != "llvm":
        raise ValueError(f"proposal source path must be under llvm: {value}")
    return "/".join(parts)


def proposal_metadata(component, selection, baseline_tests):
    """Return validated proposal metadata without changing the legacy tuple API."""
    if selection is None:
        return {"patches": (), "qualification_tests": baseline_tests,
                "before_sha256": {}, "extra_test_suites": ()}
    if component != "rust-llvm":
        raise ValueError("proposal sets are supported only for rust-llvm")

    manifest = json.loads(PROPOSAL_MANIFEST.read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("sets"), dict):
        raise ValueError("invalid Rust LLVM proposal manifest")
    entry = manifest["sets"].get(selection)
    if entry is None:
        raise ValueError(f"unknown rust-llvm proposal set: {selection}")
    patches = entry.get("patches")
    expected_tests = entry.get("qualification_tests")
    if (not isinstance(patches, list) or
            any(not isinstance(name, str) or Path(name).name != name or
                not name.endswith(".patch") for name in patches) or
            len(patches) != len(set(patches))):
        raise ValueError(f"invalid patch list for proposal set: {selection}")
    if type(expected_tests) is not int or expected_tests <= 0:
        raise ValueError(f"invalid qualification test count for proposal set: {selection}")

    before_hashes = entry.get("before_sha256", {})
    if not isinstance(before_hashes, dict):
        raise ValueError(f"invalid extra preimage hashes for proposal set: {selection}")
    normalized_hashes = {}
    for path, digest in before_hashes.items():
        path = _manifest_path(path)
        if (not isinstance(digest, str) or
                not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"invalid extra preimage hash for {path}")
        normalized_hashes[path] = digest

    suites = entry.get("extra_test_suites", [])
    if not isinstance(suites, list):
        raise ValueError(f"invalid extra test suites for proposal set: {selection}")
    normalized_suites = []
    for suite in suites:
        if not isinstance(suite, dict):
            raise ValueError(f"invalid extra test suite for proposal set: {selection}")
        paths = suite.get("paths")
        tests = suite.get("tests")
        if (not isinstance(paths, list) or not paths or
                any(not isinstance(path, str) for path in paths)):
            raise ValueError(f"invalid paths for extra test suite: {selection}")
        safe_paths = tuple(_manifest_path(path, test_path=True) for path in paths)
        if len(safe_paths) != len(set(safe_paths)):
            raise ValueError(f"duplicate path in extra test suite: {selection}")
        if type(tests) is not int or tests <= 0:
            raise ValueError(f"invalid extra test count for proposal set: {selection}")
        normalized_suites.append({"paths": safe_paths, "tests": tests})

    return {"patches": tuple(patches), "qualification_tests": expected_tests,
            "before_sha256": normalized_hashes,
            "extra_test_suites": tuple(normalized_suites)}


def proposal_selection(component, selection, baseline_tests):
    """Resolve a proposal set as the historical (patches, count) tuple."""
    metadata = proposal_metadata(component, selection, baseline_tests)
    return metadata["patches"], metadata["qualification_tests"]


def apply(source, revision, record, component="nuttx", proposal_set=None, build_checkout=False):
    if component not in SERIES:
        raise ValueError(f"unknown component: {component}")
    pins = None
    if component in PINNED_METADATA:
        pins = json.loads(PINNED_METADATA[component].read_text())
    metadata = proposal_metadata(
        component, proposal_set,
        pins["qualification_tests"]["total"] if pins else None)
    proposal_patches = metadata["patches"]
    expected_tests = metadata["qualification_tests"]

    source = Path(source).resolve(strict=True)
    if build_checkout and component != "esp-hal-3rdparty":
        raise ValueError("build checkout is only supported for isolated Espressif HAL copies")
    if (source / ".git").exists():
        if not build_checkout:
            raise ValueError("refusing to patch a Git checkout; use an archived build copy")
        # The normal builder creates and cleans this independent copy. Reject
        # worktrees/submodules whose metadata could point back to the cache.
        for option, expected in (("--show-toplevel", str(source)),
                                 ("--absolute-git-dir", str(source / ".git")),
                                 ("HEAD", revision)):
            actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", option], text=True).strip()
            if actual != expected:
                raise ValueError("Espressif HAL build checkout identity/revision mismatch")
        subprocess.run(["git", "-C", str(source), "diff", "--quiet", "--ignore-submodules=all", "HEAD"], check=True)
    if not (source / MARKERS[component]).is_file():
        raise ValueError(f"not a {component} source archive")

    provenance = {"schema": 1, "component": component,
                  "upstream_revision": revision, "patches": []}
    if pins:
        if revision != pins["revision"]:
            raise ValueError(f"{component} source revision does not match the pinned revision")
        for path, expected in pins["before_sha256"].items():
            if not (source / path).is_file() or sha256(source / path) != expected:
                raise ValueError(f"{component} source blob is incompatible or already applied: {path}")
        for path, expected in metadata["before_sha256"].items():
            if not (source / path).is_file() or sha256(source / path) != expected:
                raise ValueError(f"{component} extra source blob is incompatible: {path}")
        provenance["qualification"] = pins["status"]
        provenance["rust_revision"] = pins["rust_revision"]
        provenance["proposal_set"] = proposal_set
        provenance["qualification_tests"] = expected_tests
        if metadata["before_sha256"] or metadata["extra_test_suites"]:
            provenance["extra_qualification"] = {
                "before_sha256": metadata["before_sha256"],
                "test_suites": [
                    {"paths": list(suite["paths"]),
                     "expected_passes": suite["tests"],
                     "count_scope": "llvm-lit Passed count"}
                    for suite in metadata["extra_test_suites"]
                ],
            }
    patch_series = [(name, False) for name in SERIES[component]]
    patch_series.extend((name, True) for name in proposal_patches)
    for name, is_proposal in patch_series:
        patch_root = ROOT / "upstream" / component / ("proposals" if is_proposal else "patches")
        patch = patch_root / name
        paths = [line.split("\t", 2)[2] for line in subprocess.check_output(
            ["git", "apply", "--numstat", str(patch)],
            cwd=source, env=git_env(source), text=True,
        ).splitlines()]
        if not paths:
            raise ValueError(f"patch {name} has no source changes")
        check = run_git_apply(source, "--check", "--whitespace=error", patch=patch)
        if check.returncode:
            raise ValueError(f"patch {name} is incompatible or already applied: {check.stderr.strip()}")
        before = {path: sha256(source / path) if (source / path).is_file() else None
                  for path in paths}
        result = run_git_apply(source, "--whitespace=error", patch=patch)
        if result.returncode:
            raise RuntimeError(f"failed to apply {name}: {result.stderr.strip()}")
        entry = {
            "name": name,
            "sha256": sha256(patch),
            "files": {path: {"before": before[path], "after": sha256(source / path)}
                      for path in paths},
        }
        if is_proposal:
            entry["proposal"] = True
        provenance["patches"].append(entry)
    Path(record).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--component", choices=SERIES, default="nuttx")
    parser.add_argument("--proposal-set", help="opt in to a named Rust LLVM proposal set")
    parser.add_argument("--build-checkout", action="store_true",
                        help="patch an isolated Espressif HAL build copy, never its source cache")
    args = parser.parse_args()
    apply(args.source, args.revision, args.record, args.component, args.proposal_set, args.build_checkout)
