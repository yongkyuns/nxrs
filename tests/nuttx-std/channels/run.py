#!/usr/bin/env python3
"""Run the native channel body on a host; does not qualify target IRQs/parking."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def run(prepared, out):
    out.mkdir(parents=True)
    sdk = out / "toolchain"
    original = prepared.resolve(strict=True) / "toolchain"
    if sys.platform == "darwin":
        subprocess.run(["cp", "-cpR", original, sdk], check=True)
    elif sys.platform.startswith("linux"):
        subprocess.run(["cp", "--reflink=auto", "-a", original, sdk], check=True)
    else:
        shutil.copytree(original, sdk, symlinks=True)
    library = sdk / "lib/rustlib/src/rust/library"
    module = library / "std/src/sync/mpmc/mod.rs"
    body = library / "std/src/sys/sync/mpmc/nuttx/mod.rs"
    if not module.resolve().is_relative_to(sdk.resolve()):
        raise ValueError("copied SDK source escapes the private fixture")
    patch = ROOT / "upstream/rust-std/patches/0007-nuttx-bounded-channel.patch"
    subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                   cwd=sdk, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(out)}, check=True)
    before = body.read_bytes()
    selector = '#[cfg_attr(target_os = "nuttx", path = "../../sys/sync/mpmc/nuttx/mod.rs")]'
    text = module.read_text()
    if text.count(selector) != 1:
        raise ValueError("expected one NuttX channel selector")
    module.write_text(text.replace(selector, '#[path = "../../sys/sync/mpmc/nuttx/mod.rs"]'))
    crate = out / "controls"
    crate.mkdir()
    for name in ["Cargo.toml", "Cargo.lock", "src"]:
        if (HERE / name).is_dir():
            shutil.copytree(HERE / name, crate / name)
        else:
            shutil.copyfile(HERE / name, crate / name)
    compiler = sdk / "bin" / ("rustc.exe" if os.name == "nt" else "rustc")
    identity = subprocess.check_output([compiler, "-vV"], text=True)
    target = next(line.removeprefix("host: ") for line in identity.splitlines() if line.startswith("host: "))
    env = {**os.environ, "RUSTC": str(compiler), "RUSTC_BOOTSTRAP": "1", "RUSTFLAGS": "-C panic=abort"}
    command = ["cargo", "build", "--locked", "--release", "--manifest-path", str(crate / "Cargo.toml"),
               "--target", target, "--target-dir", str(out / "target"), "-Zbuild-std=std,panic_abort",
               "-Zbuild-std-features=panic_immediate_abort", "--message-format=json"]
    with (out / "build.jsonl").open("w") as messages, (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, env=env, stdout=messages, stderr=log, timeout=180, check=True)
    artifacts = [json.loads(line) for line in (out / "build.jsonl").read_text().splitlines()]
    selected = [x for x in artifacts if x.get("reason") == "compiler-artifact" and x["target"]["name"] == "std"]
    if len(selected) != 1 or selected[0]["fresh"]:
        raise ValueError("fixture requires a fresh std build")
    deps = "\n".join(p.read_text() for p in (out / "target").rglob("std-*.d"))
    paths = {Path(re.sub(r"\\([ #:\\])", r"\1", word.rstrip(":"))).resolve()
             for word in re.findall(r"(?:\\[^\r\n]|[^\s\\])+", deps) if "/std/src/" in word}
    if body.resolve() not in paths or body.read_bytes() != before:
        raise ValueError("native queue body was changed or not compiled")
    executable = out / "target" / target / "release/completed-transfer-controls"
    if os.name == "nt":
        executable = executable.with_suffix(".exe")
    with (out / "controls.log").open("w") as log:
        subprocess.run([executable], stdout=log, stderr=subprocess.STDOUT, timeout=60, check=True)
    if sum(line.startswith("PASS ") for line in (out / "controls.log").read_text().splitlines()) != 9:
        raise ValueError("incomplete channel control suite")
    (out / "qualification.json").write_text(json.dumps({"passed": True, "controls": 9,
        "native_body_sha256": hashlib.sha256(before).hexdigest(), "fresh_std": True,
        "fixture_only_selector_override": True, "target_hardware_qualified": False}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-sdk", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.prepared_sdk, args.out.resolve())
