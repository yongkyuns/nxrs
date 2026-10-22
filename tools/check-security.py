#!/usr/bin/env python3
"""Fail closed on mutable CI actions, invalid exceptions and RustSec audit errors."""
import argparse
import datetime as dt
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
AUDIT_VERSION = "0.22.2"
WORKSPACES = {"root": "Cargo.lock", "gateway": "gateway/Cargo.lock"}


def exceptions(path, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    entries = tomllib.loads(path.read_text())
    if set(entries) != {"exception"} or not isinstance(entries["exception"], list):
        raise ValueError("invalid advisory exception table")
    result = {name: [] for name in WORKSPACES}
    seen = set()
    for entry in entries["exception"]:
        required = {"id", "workspace", "owner", "reason", "issue", "expires"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("exception requires id, workspace, owner, reason, issue and expires")
        if not all(isinstance(entry[key], str) and entry[key].strip() for key in required):
            raise ValueError("exception fields must be nonempty strings")
        if (not re.fullmatch(r"RUSTSEC-\d{4}-\d{4}", entry["id"])
                or entry["workspace"] not in WORKSPACES
                or not re.fullmatch(r"https://github.com/yongkyuns/nxrs/issues/[1-9]\d*", entry["issue"])):
            raise ValueError("invalid exception identity, scope or tracking issue")
        expiry = dt.date.fromisoformat(entry["expires"])
        if not today < expiry <= today + dt.timedelta(days=90):
            raise ValueError("exception expired or exceeds the 90-day review window")
        identity = (entry["workspace"], entry["id"])
        if identity in seen:
            raise ValueError("duplicate scoped advisory exception")
        seen.add(identity)
        result[entry["workspace"]].append(entry["id"])
    return result


def action_errors(text, name):
    """Check literal action steps; reject unsupported dynamic/multiline uses values."""
    errors = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:-\s*)?uses:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match[1].split(" #", 1)[0].strip().strip("\"'")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", value):
            errors.append(f"{name}:{index + 1}: action must use a full commit SHA")
        if value.startswith("actions/checkout@"):
            step = []
            for following in lines[index + 1:]:
                if re.match(r"^\s*-\s", following):
                    break
                step.append(following)
            if not any(re.fullmatch(r"\s*persist-credentials:\s*false\s*(?:#.*)?", v) for v in step):
                errors.append(f"{name}:{index + 1}: checkout must disable credential persistence")
    return errors


def policy(root):
    exceptions(root / "security/advisory-exceptions.toml")
    errors = []
    workflows = sorted((root / ".github/workflows").glob("*.y*ml"))
    if not workflows:
        raise ValueError("no workflows found")
    for workflow in workflows:
        errors.extend(action_errors(workflow.read_text(), str(workflow.relative_to(root))))
    if errors:
        raise ValueError("\n".join(errors))
    print(f"Security policy: {len(workflows)} workflows pinned; exception policy valid")


def audit(root, workspace, executable, database=None):
    ignored = exceptions(root / "security/advisory-exceptions.toml")[workspace]
    lock = root / WORKSPACES[workspace]
    if not lock.is_file() or lock.is_symlink():
        raise ValueError("committed regular workspace lockfile required")
    version = subprocess.run([executable, "--version"], check=True,
                             capture_output=True, text=True).stdout.strip()
    if version != f"cargo-audit {AUDIT_VERSION}":
        raise ValueError(f"cargo-audit {AUDIT_VERSION} required")
    command = [executable, "audit", "--file", str(lock), "--deny", "unsound"]
    if database is not None:
        command += ["--db", str(database.resolve())]
    for advisory in ignored:
        command += ["--ignore", advisory]
    # Do not load an unrelated checkout's audit.toml or generate a missing lockfile.
    with tempfile.TemporaryDirectory(prefix="nxrs-audit-") as directory:
        config = Path(directory) / ".cargo"
        config.mkdir()
        (config / "audit.toml").write_text("[advisories]\nignore = []\n")
        result = subprocess.run(command, cwd=directory, check=False)
    if result.returncode != 0:
        raise ValueError(f"{workspace}: advisory audit failed (exit {result.returncode})")
    print(f"{workspace}: RustSec audit passed; reviewed exceptions: {len(ignored)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("policy")
    scan = commands.add_parser("audit")
    scan.add_argument("--workspace", choices=WORKSPACES, required=True)
    scan.add_argument("--auditor", default="cargo-audit")
    scan.add_argument("--database", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "policy":
            policy(ROOT)
        else:
            audit(ROOT, args.workspace, args.auditor, args.database)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Security check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
