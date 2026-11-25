#!/usr/bin/env python3
"""Build and run fresh-process host RAM controls; never reports MCU RAM totals."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess

from report import parse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BINS = ("ram-baseline", "ram-threads", "ram-std-inbox", "ram-crossbeam-select")
COUNTS = (0, 1, 5, 10, 20)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    paths = list(HERE.glob("*.py")) + list(HERE.glob("Cargo.*"))
    paths += list((HERE / "src").rglob("*.rs")) + list((HERE / "ffi").glob("*.c"))
    for component in (ROOT / "shared/messaging", ROOT / "tests/allocation-probe"):
        paths += list(component.rglob("*.rs")) + [component / "Cargo.toml"]
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)}


def run(out, stack_bytes, repeats):
    out.mkdir(parents=True, exist_ok=False)
    command = ["cargo", "build", "--locked", "--release", "--manifest-path", str(HERE / "Cargo.toml"),
               "--bins", "--target-dir", str(out / "target"), "--message-format=json"]
    with (out / "build.jsonl").open("w") as messages, (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, stdout=messages, stderr=log, check=True)
    artifacts = [json.loads(line) for line in (out / "build.jsonl").read_text().splitlines()]
    executables = {item["target"]["name"]: Path(item["executable"])
                   for item in artifacts if item.get("reason") == "compiler-artifact" and item.get("executable")}
    if set(executables) != set(BINS):
        raise ValueError("Cargo did not produce exactly the four control binaries")
    cases = [("ram-baseline", [])]
    for count in COUNTS:
        cases.append(("ram-threads", ["threads", str(count), str(stack_bytes)]))
        for binary in ("ram-std-inbox", "ram-crossbeam-select"):
            for kind in ("queues", "services"):
                cases.append((binary, [kind, str(count), str(stack_bytes)]))
    reports = []
    for repeat in range(repeats):
        for binary, arguments in cases:
            label = "-".join([str(repeat + 1), binary, *arguments])
            log = out / f"{label}.log"
            with log.open("w") as output:
                subprocess.run([str(executables[binary]), *arguments], cwd=ROOT,
                               stdout=output, stderr=subprocess.STDOUT, timeout=30, check=True)
            report = parse(log.read_text(), "host-functional")
            report.update(binary=binary, repeat=repeat + 1, log=log.name)
            reports.append(report)
    result = {"schema": 1, "domain": "host-functional", "physical_hardware_qualified": False,
              "rust_global_allocator_requested_bytes_only": True,
              "posix_stack_reservations_not_measured": True,
              "counts": COUNTS, "stack_request_per_worker": stack_bytes,
              "repeats": repeats, "build_command": command,
              "rustc": subprocess.check_output([os.environ.get("RUSTC", "rustc"), "-vV"], text=True),
              "cargo": subprocess.check_output(["cargo", "-V"], text=True).strip(),
              "host": platform.platform(),
              "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_sha256": source_hashes(),
              "executable_sha256": {name: digest(path) for name, path in executables.items()},
              "invocations": reports}
    (out / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Passed {len(reports)} fresh-process host controls; {out / 'report.json'}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stack-bytes", type=int, default=65536,
                        help="explicit host thread stack request; not a measured RAM size")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if args.stack_bytes < 16384 or args.repeats < 1:
        parser.error("host stack request must be at least 16384; repeats must be positive")
    run(args.out.resolve(), args.stack_bytes, args.repeats)


if __name__ == "__main__":
    main()
