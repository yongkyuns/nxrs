"""Authenticate the scheduler and prepared parker used by FIFO witnesses.

These checks bind the narrow source argument in FIFO-SCHEDULING-AUDIT.md. They
do not change the kernel, install sources, or establish that an image executed.
The builder checks the actual prepared kernel again after compilation; replay
checks independent retained copies and the same immutable source map.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat

KERNEL_REVISION = "2b5c0de5e933281c209f954a63b99cf10c132346"
KERNEL_SOURCE_SHA256 = {
    "sched/sched/sched.h": "57b4cc2d873a57f9a2410598d953b7ab0bf0cb21eb11d019d29b29407885b21a",
    "sched/sched/sched_addreadytorun.c": "9969e9395eb1cb91127229bd6b65e2f12b7078f41b420067bf0ae2d82f579fa0",
    "sched/sched/sched_removereadytorun.c": "03bb1aa9b17867c524c729440343ba6ded69978f40baaee137fa1de5d5aa7d0a",
    "sched/sched/sched_mergepending.c": "dbe356d1068dd0ee3e69f3cf782da6619ccda1e0cdcfd61dafa7e576e5983e02",
    "sched/sched/sched_unlock.c": "d2aabeb580c2762fc8b859b3bb3d45a8265b5b274830b055b059bab7eeb5b07e",
    "sched/sched/sched_processtick.c": "e2f60009edfac6998d905a16ea37a01e5fc3058cfe0881c25f5af5811bec03d4",
    "sched/sched/sched_processtickless.c": "8b147e08d18974fd56e508227262b2f6f8d5b724a1abd13d706e6ec0c1af550e",
    "sched/semaphore/sem_post.c": "ade456e5fd87fb2472e8c84fb3a528125b854bc3c7cf9e86ce8660d94727d7ee",
    "sched/semaphore/sem_wait.c": "a4f4bb20bf81460ba025f38758f9500a7dec45e6c1bd02a58b4f257ea78f84e6",
    "sched/semaphore/sem_waitirq.c": "f51a033e67f4e2d65102ddff934e400841aea0d1360e646603b24aa6f2f96657",
}

# These supplement mutex_analyze.validate_std_sources. The pthread file is the
# common PREPARED source, after prepare-std.py's NuttX mutex initialization fix.
# The dispatch differs between the two pinned SDKs; the prepared implementation
# is byte-identical. Paths are relative to the retained complete library ledger.
STD_SOURCE_SHA256 = {
    "std/src/sys/sync/thread_parking/mod.rs": "4aea2e6fd87d71bd6572a232af6c56bcc5615baacfcc64e2b27d88839d767328",
    "std/src/sys/sync/thread_parking/pthread.rs": "8779b59ac72e52511db47fa44c4f1bdbc9840046e9e4c98c6a63f6acb5b920f5",
}
ESP_STD_SOURCE_SHA256 = {
    **STD_SOURCE_SHA256,
    "std/src/sys/sync/thread_parking/mod.rs": "64bfa00edd937fafb302a6655434718acecb736dbb3996d73904d4ef978291c5",
}


def _directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise ValueError(f"missing FIFO kernel source directory: {path}") from error
    if not stat.S_ISDIR(mode):
        raise ValueError(f"FIFO kernel source directory is not a regular directory: {path}")


def _read_sources(kernel_root: Path) -> dict[str, bytes]:
    root = Path(kernel_root)
    _directory(root)
    files = {}
    for relative, expected in KERNEL_SOURCE_SHA256.items():
        path = root / relative
        for directory in reversed(path.parent.relative_to(root).parents):
            _directory(root / directory)
        _directory(path.parent)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"FIFO kernel source is not an independent regular file: {relative}")
            if info.st_size > 2 * 1024 * 1024:
                raise ValueError(f"FIFO kernel source exceeds its size bound: {relative}")
            data = path.read_bytes()
        except OSError as error:
            raise ValueError(f"missing or unreadable FIFO kernel source: {relative}") from error
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"changed audited FIFO kernel source: {relative}")
        files[relative] = data
    return files


def verify_kernel(kernel_root: Path) -> dict[str, str]:
    """Check the actual kernel bytes without changing the source tree."""
    files = _read_sources(kernel_root)
    return {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}


def prepare_kernel(kernel_root: Path, destination: Path) -> dict[str, str]:
    """Copy authenticated sources into a new, independent retained directory."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("retained FIFO kernel output must be new")
    # Authenticate every input before creating any output or writing a byte.
    files = _read_sources(kernel_root)
    destination.mkdir(parents=True)
    for relative, data in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return validate_retained_kernel(destination, dict(KERNEL_SOURCE_SHA256), KERNEL_REVISION)


def validate_retained_kernel(destination: Path, source_map: dict, revision: str) -> dict[str, str]:
    """Reject resealed source drift, extra entries, links and unknown kernels."""
    if revision != KERNEL_REVISION:
        raise ValueError("unqualified FIFO kernel revision")
    if not isinstance(source_map, dict) or source_map != KERNEL_SOURCE_SHA256:
        raise ValueError("FIFO kernel source map differs from audited pins")
    destination = Path(destination)
    _directory(destination)
    expected_files = set(KERNEL_SOURCE_SHA256)
    expected_directories = {
        parent.as_posix() for path in expected_files
        for parent in Path(path).parents if parent != Path(".")
    }
    actual_files = set()
    actual_directories = set()
    for path in destination.rglob("*"):
        relative = path.relative_to(destination).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            actual_directories.add(relative)
        elif stat.S_ISREG(mode):
            actual_files.add(relative)
        else:
            raise ValueError(f"retained FIFO kernel source contains a link or nonregular entry: {relative}")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("retained FIFO kernel source inventory has missing or extra entries")
    return verify_kernel(destination)


def validate_std_sources(library: dict, sdk: str) -> None:
    """Require the audited SDK dispatch and common prepared pthread parker."""
    if not isinstance(library, dict):
        raise ValueError("FIFO parker source ledger must be an object")
    if sdk == "nightly-2025-09-15":
        expected = STD_SOURCE_SHA256
    elif sdk == "esp-1.90.0.0":
        expected = ESP_STD_SOURCE_SHA256
    else:
        raise ValueError("unqualified SDK for FIFO parker sources")
    for path, digest in expected.items():
        if library.get(path) != digest:
            raise ValueError(f"missing or changed audited FIFO parker source: {path}")
