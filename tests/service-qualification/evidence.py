"""Shared success gates for private captures and public qualification records."""
import re


def kernel_header_identity(builds):
    """Require valid, matching SHA-256 identities for compiled kernel headers."""
    if not isinstance(builds, dict) or set(builds) != {"c", "rust"}:
        raise ValueError("paired C/Rust builds required")
    headers = []
    for language in ("c", "rust"):
        build = builds[language]
        header = build.get("kernel_header_sha256") if isinstance(build, dict) else None
        if not isinstance(header, str) or re.fullmatch(r"[0-9a-f]{64}", header) is None:
            raise ValueError("missing/invalid kernel header SHA-256: " + language)
        headers.append(header)
    if headers[0] != headers[1]:
        raise ValueError("C/Rust kernel header identities differ")
    return headers[0]


def require_restored(report):
    """Historical records may omit restoration_error, never ignore its value."""
    if (report.get("failure") is not None or report.get("restoration_error") is not None or
            report.get("restoration_verified") is not True):
        raise ValueError("successful capture and verified restoration required")
