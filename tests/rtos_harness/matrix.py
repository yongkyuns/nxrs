"""Common ordering and restore-on-exit policy for local device matrices."""
from contextlib import contextmanager
import hashlib
import json


def backup_identity(path):
    """Require a private, complete board backup before a matrix can flash."""
    if not path.is_file() or path.stat().st_size != 16_777_216:
        raise ValueError("a complete 16 MiB local backup is required before flashing")
    if path.stat().st_mode & 0o077:
        raise ValueError("firmware backup must be private (no group/other permissions)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rotated_cases(cases, block):
    offset = block % len(cases)
    return list(cases[offset:]) + list(cases[:offset])


@contextmanager
def restored_session(record, report, backup, *, restore, identity=backup_identity):
    """Persist failures and verify restoration without hiding a run failure.

    Workload runners own preflight and their numeric records. This common exit
    path owns only board restoration and the private matrix envelope.
    """
    try:
        yield
    except BaseException as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            restore()
            if identity(backup) != record["backup_sha256"]:
                raise ValueError("local firmware backup changed during restore")
            record["restore_verified"] = True
        except Exception as exc:
            record["restore_error"] = f"{type(exc).__name__}: {exc}"
            if record["failure"] is None:
                record["failure"] = record["restore_error"]
        finally:
            report.write_text(json.dumps(record, indent=2) + "\n")
            report.chmod(0o600)
    if record["restore_error"]:
        raise RuntimeError(f"firmware restoration failed: {record['restore_error']}")
