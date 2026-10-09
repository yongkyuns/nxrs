#!/usr/bin/env python3
"""Apply an explicitly selected Rust std RFC to a private source archive copy."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_DIR = ROOT / "upstream/rust-std/proposals"
PROPOSAL_MANIFEST = PROPOSAL_DIR / "series.json"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 250_000


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_source_path(value):
    """Validate a path rooted at the extracted Rust library directory."""
    if (not isinstance(value, str) or not value or "\\" in value or
            value.startswith("/") or re.match(r"^[A-Za-z]:", value)):
        raise ValueError(f"unsafe Rust std proposal path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..", ".git") for part in parts):
        raise ValueError(f"unsafe Rust std proposal path: {value!r}")
    if len(parts) < 3 or parts[:2] != ["std", "src"]:
        raise ValueError(f"Rust std proposal path must be under std/src: {value}")
    return "/".join(parts)


def _load_proposal(name, manifest_path=None, proposal_dir=None):
    manifest_path = Path(manifest_path or PROPOSAL_MANIFEST)
    proposal_dir = Path(proposal_dir or PROPOSAL_DIR)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != 1 or not isinstance(manifest.get("proposals"), dict):
        raise ValueError("invalid Rust std proposal manifest")
    entry = manifest["proposals"].get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"unknown Rust std proposal: {name}")
    if entry.get("state") != "rfc" or entry.get("qualification") != "unqualified":
        raise ValueError(f"Rust std proposal is not an unqualified RFC: {name}")
    if entry.get("selection_scope") != "evaluation-preparation-only":
        raise ValueError(f"Rust std proposal selection must remain preparation-only: {name}")
    revision = entry.get("source_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"invalid pinned Rust source revision for proposal: {name}")
    if entry.get("target_os") != "nuttx":
        raise ValueError(f"unexpected target selection for Rust std proposal: {name}")

    patch = entry.get("patch")
    if not isinstance(patch, dict):
        raise ValueError(f"invalid patch record for Rust std proposal: {name}")
    patch_name = patch.get("file")
    patch_digest = patch.get("sha256")
    if (not isinstance(patch_name, str) or Path(patch_name).name != patch_name or
            "\\" in patch_name or
            not patch_name.endswith(".patch") or
            not isinstance(patch_digest, str) or
            not re.fullmatch(r"[0-9a-f]{64}", patch_digest)):
        raise ValueError(f"invalid patch pin for Rust std proposal: {name}")
    patch_path = proposal_dir / patch_name
    if (patch_path.is_symlink() or not patch_path.is_file() or
            patch_path.resolve().parent != proposal_dir.resolve()):
        raise ValueError(f"proposal patch is missing or escapes its directory: {patch_name}")
    patch_bytes = patch_path.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != patch_digest:
        raise ValueError(f"proposal patch hash mismatch: {patch_name}")

    before = entry.get("before_sha256")
    if not isinstance(before, dict) or not before:
        raise ValueError(f"missing pinned source hashes for Rust std proposal: {name}")
    normalized = {}
    for path, digest in before.items():
        path = safe_source_path(path)
        if (not isinstance(digest, str) or
                not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"invalid source hash for Rust std proposal path: {path}")
        normalized[path] = digest
    if len(normalized) != len(before):
        raise ValueError(f"duplicate normalized source path for proposal: {name}")

    return {
        "name": name,
        "state": entry["state"],
        "qualification": entry["qualification"],
        "selection_scope": entry["selection_scope"],
        "source_revision": revision,
        "target_os": entry["target_os"],
        "patch_name": patch_name,
        "patch_bytes": patch_bytes,
        "patch_sha256": patch_digest,
        "before_sha256": normalized,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def _archive_member_path(name, is_directory):
    if not isinstance(name, str) or "\x00" in name or "\\" in name:
        raise ValueError(f"unsafe archive member path: {name!r}")
    if name.startswith("./"):
        name = name[2:]
    if name in ("", ".") and is_directory:
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"absolute archive member path: {name!r}")
    if is_directory and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if any(part in ("", ".", "..", ".git") for part in parts):
        raise ValueError(f"unsafe archive member path: {name!r}")
    return PurePosixPath(*parts)


def _extract_snapshot(archive_path, private_archive, destination):
    size = archive_path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise ValueError("Rust source archive exceeds the compressed size limit")
    archive_digest = hashlib.sha256()
    copied_bytes = 0
    with archive_path.open("rb") as source, private_archive.open("xb") as copy:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            copied_bytes += len(chunk)
            if copied_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError("Rust source archive exceeds the compressed size limit")
            archive_digest.update(chunk)
            copy.write(chunk)
    archive_sha256 = archive_digest.hexdigest()

    seen = set()
    total_bytes = 0
    try:
        with private_archive.open("rb") as archive_file:
            archive_context = tarfile.open(fileobj=archive_file, mode="r:*")
            with archive_context as archive:
                for index, member in enumerate(archive):
                    if index >= MAX_ARCHIVE_MEMBERS:
                        raise ValueError("Rust source archive exceeds the member count limit")
                    relative = _archive_member_path(member.name, member.isdir())
                    if relative is None:
                        continue
                    key = relative.as_posix()
                    if key in seen:
                        raise ValueError(f"duplicate archive member path: {key}")
                    seen.add(key)
                    target = destination.joinpath(*relative.parts)
                    if not target.resolve(strict=False).is_relative_to(destination.resolve()):
                        raise ValueError(f"archive member escapes private snapshot: {key}")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if (not member.isfile() or
                            member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or
                            getattr(member, "sparse", None)):
                        raise ValueError(f"archive links and special files are not allowed: {key}")
                    total_bytes += member.size
                    if member.size < 0 or total_bytes > MAX_EXTRACTED_BYTES:
                        raise ValueError("Rust source archive exceeds the extracted size limit")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"could not read archive member: {key}")
                    with source, target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError(f"truncated archive member: {key}")
                            output.write(chunk)
                            remaining -= len(chunk)
                    target.chmod(0o644)
    except tarfile.TarError as error:
        raise ValueError(f"invalid Rust source tar archive: {error}") from error
    return archive_sha256


def _git_env(snapshot):
    # The private copy can be nested in this repository; stop Git discovery
    # above its private staging parent before running git apply.
    environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        environment.pop(key, None)
    environment["GIT_CEILING_DIRECTORIES"] = str(snapshot.parent)
    return environment


def _patch_paths(snapshot, patch_path):
    result = subprocess.run(
        ["git", "apply", "--numstat", str(patch_path)], cwd=snapshot,
        env=_git_env(snapshot), text=True, capture_output=True,
    )
    if result.returncode:
        raise ValueError(f"invalid Rust std proposal patch: {result.stderr.strip()}")
    paths = []
    for row in result.stdout.splitlines():
        fields = row.split("\t", 2)
        if len(fields) != 3:
            raise ValueError("could not parse Rust std proposal patch paths")
        paths.append(safe_source_path(fields[2]))
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("proposal patch must change distinct pinned Rust std files")
    return set(paths)


def _source_file(snapshot, relative):
    path = snapshot.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"pinned Rust std source file is missing or not regular: {relative}")
    if not path.resolve(strict=True).is_relative_to(snapshot.resolve(strict=True)):
        raise ValueError(f"pinned Rust std source path escapes private snapshot: {relative}")
    return path


def apply_proposal(snapshot_archive, output, proposal, source_revision,
                   *, manifest_path=None, proposal_dir=None):
    """Extract, verify, patch, and record a private Rust library snapshot.

    The input archive and any installed SDK remain untouched. ``output`` is a
    new directory containing ``snapshot/`` and ``provenance.json``.
    """
    selected = _load_proposal(proposal, manifest_path, proposal_dir)
    if source_revision != selected["source_revision"]:
        raise ValueError("Rust library revision does not match the proposal pin")

    archive_path = Path(snapshot_archive)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("Rust source snapshot must be a regular archive file")
    archive_path = archive_path.resolve(strict=True)

    requested_output = Path(output)
    if requested_output.name in ("", ".", ".."):
        raise ValueError("output must name a new private snapshot directory")
    parent = requested_output.parent.resolve(strict=True)
    destination = parent / requested_output.name
    if os.path.lexists(destination):
        raise ValueError(f"private snapshot output already exists: {destination.name}")

    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.std-rfc-", dir=parent))
    try:
        snapshot = staging / "snapshot"
        snapshot.mkdir()
        private_archive = staging / "source.tar"
        archive_sha256 = _extract_snapshot(archive_path, private_archive, snapshot)
        private_archive.unlink()

        if any(path.name == ".git" for path in snapshot.rglob(".git")):
            raise ValueError("refusing a Rust source snapshot containing .git")

        expected_paths = set(selected["before_sha256"])
        private_patch = staging / "proposal.patch"
        private_patch.write_bytes(selected["patch_bytes"])
        changed_paths = _patch_paths(snapshot, private_patch)
        if changed_paths != expected_paths:
            raise ValueError("proposal patch paths do not match the pinned source files")

        before = {}
        for relative, expected in selected["before_sha256"].items():
            path = _source_file(snapshot, relative)
            actual = sha256_file(path)
            if actual != expected:
                raise ValueError(
                    f"Rust std source is incompatible or already patched: {relative}")
            before[relative] = actual

        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=error", str(private_patch)],
            cwd=snapshot, env=_git_env(snapshot), text=True, capture_output=True,
        )
        if check.returncode:
            raise ValueError(
                f"Rust std proposal is incompatible or already applied: {check.stderr.strip()}")
        applied = subprocess.run(
            ["git", "apply", "--whitespace=error", str(private_patch)],
            cwd=snapshot, env=_git_env(snapshot), text=True, capture_output=True,
        )
        if applied.returncode:
            raise RuntimeError(f"failed to apply Rust std proposal: {applied.stderr.strip()}")
        private_patch.unlink()

        files = {}
        for relative, original in before.items():
            path = _source_file(snapshot, relative)
            files[relative] = {
                "before_sha256": original,
                "after_sha256": sha256_file(path),
            }
        provenance = {
            "schema": 1,
            "proposal": selected["name"],
            "proposal_state": selected["state"],
            "qualification": selected["qualification"],
            "selection_scope": selected["selection_scope"],
            "target_os": selected["target_os"],
            "source_revision": selected["source_revision"],
            "snapshot_archive_sha256": archive_sha256,
            "proposal_manifest_sha256": selected["manifest_sha256"],
            "patch": {
                "file": selected["patch_name"],
                "sha256": selected["patch_sha256"],
            },
            "files": files,
        }
        (staging / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n")
        if os.path.lexists(destination):
            raise ValueError(f"private snapshot output appeared during application: {destination.name}")
        staging.rename(destination)
        return provenance
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-archive", required=True, type=Path,
                        help="tar archive rooted at the Rust library source directory")
    parser.add_argument("--output", required=True, type=Path,
                        help="new private directory for snapshot and provenance")
    parser.add_argument("--proposal", required=True,
                        help="explicit optional RFC name from the std proposal manifest")
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    try:
        provenance = apply_proposal(
            args.snapshot_archive, args.output, args.proposal, args.source_revision)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(provenance, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
