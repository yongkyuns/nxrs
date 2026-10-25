#!/usr/bin/env python3
"""Fail closed on mutable CI actions, invalid exceptions and RustSec audit errors."""
import argparse
import ast
import datetime as dt
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
AUDIT_VERSION = "0.22.2"
WORKSPACES = {"root": "Cargo.lock", "gateway": "gateway/Cargo.lock", "fuzz": "gateway/fuzz/Cargo.lock"}


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


def fuzz_dependencies(root):
    def identities(path):
        lock = tomllib.loads(path.read_text())
        return {(item["name"], item["version"], item.get("source"), item.get("checksum"))
                for item in lock.get("package", []) if "source" in item}
    if identities(root / "gateway/Cargo.lock") - identities(root / "gateway/fuzz/Cargo.lock"):
        raise ValueError("fuzz workspace must retain exact gateway dependency identities")


IDENTITY_SCRIPTS = {"host": "qualify.py", "identity": "qualify_nuttx.py", "tls": "qualify_tls_nuttx.py"}


def identity_declarations(root, target, field="INPUTS", visiting=()):
    """Read the actual literal source declarations without importing native tools."""
    if target in visiting or field not in {"INPUTS", "TREE_INPUTS"}:
        raise ValueError("invalid identity source declaration")
    path = root / "tools/device-identity" / IDENTITY_SCRIPTS[target]
    tree = ast.parse(path.read_text())
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(name, ast.Name) and name.id == field for name in node.targets)]
    if not values and target == "host" and field == "TREE_INPUTS":
        return set()
    if len(values) != 1:
        raise ValueError("one identity source declaration required")
    names = set()
    for node in ast.walk(values[0]):
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", node.value):
                raise ValueError("literal identity source path required")
            path = Path(node.value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("relative identity source path required")
            names.add(node.value)
        elif isinstance(node, ast.Name) and node.id not in {"tuple", "dict", "identity", "base"}:
            raise ValueError("unknown identity source reference")
        elif isinstance(node, ast.Attribute):
            if node.attr == field and isinstance(node.value, ast.Name) and node.value.id in {"identity", "base"}:
                parent = {"identity": "host", "base": "identity"}[node.value.id]
                names.update(identity_declarations(root, parent, field, (*visiting, target)))
            elif not (node.attr == "fromkeys" and isinstance(node.value, ast.Name) and node.value.id == "dict"):
                raise ValueError("unknown identity source expression")
    return names


def identity_path_filters(text, event):
    """Accept this workflow's positive literal/recursive path-filter subset."""
    on = re.search(r"^on:\n(.*?)(?=^\S|\Z)", text, re.M | re.S)
    block = re.search(r"^  " + re.escape(event) + r":\n(.*?)(?=^  [A-Za-z_]+:|\Z)", on[1], re.M | re.S) if on else None
    if not block or re.search(r"^    paths-ignore:", block[1], re.M):
        raise ValueError("identity workflow requires positive path filters")
    paths = re.search(r"^    paths:\n(.*)", block[1], re.M | re.S)
    if not paths:
        raise ValueError("identity workflow requires path filters")
    result = []
    for line in paths[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith("      - "):
            raise ValueError("unsupported identity path-filter structure")
        value = ast.literal_eval(line.removeprefix("      - "))
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+(?:/\*\*)?", value):
            raise ValueError("unsupported identity path-filter pattern")
        result.append(value)
    if not result:
        raise ValueError("empty identity workflow path filters")
    return result


def identity_filter_errors(root):
    errors = []
    for workflow, target in [("device-identity.yml", "host"), ("device-identity-nuttx.yml", "tls")]:
        names = identity_declarations(root, target)
        for directory in identity_declarations(root, target, "TREE_INPUTS"):
            # Keep a future/new file covered even if a declared tree is empty.
            names.add(directory + "/__qualification_input__")
            names.update(path.relative_to(root).as_posix() for path in (root / directory).rglob("*") if path.is_file())
        text = (root / ".github/workflows" / workflow).read_text()
        for event in ("pull_request", "push"):
            patterns = identity_path_filters(text, event)
            missing = sorted(name for name in names if not any(name == pattern or
                pattern.endswith("/**") and name.startswith(pattern[:-2]) for pattern in patterns))
            errors.extend(f"{workflow} {event} excludes qualified input {name}" for name in missing)
    return errors


def policy(root):
    exceptions(root / "security/advisory-exceptions.toml")
    fuzz_dependencies(root)
    errors = []
    workflows = sorted((root / ".github/workflows").glob("*.y*ml"))
    if not workflows:
        raise ValueError("no workflows found")
    for workflow in workflows:
        errors.extend(action_errors(workflow.read_text(), str(workflow.relative_to(root))))
    errors.extend(identity_filter_errors(root))
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
