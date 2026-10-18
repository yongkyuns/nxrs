"""Read the deliberately small, identity-free BLE scan witness protocol."""

from dataclasses import dataclass
import re
import time


_MAX_LINE = 8192
_MAX_MATCHES = 32
_PROMPT = b"nsh> "
_READY = re.compile(rb"NXRS_BLE_WITNESS_READY elapsed_ms=([0-9]+)\Z")
_SEEN = re.compile(rb"NXRS_BLE_WITNESS_SEEN elapsed_ms=([0-9]+)\Z")
_DONE = re.compile(
    rb"NXRS_BLE_WITNESS_DONE matches=([0-9]+) limited=(true|false) shutdown=joined\Z"
)
_STATUS = re.compile(rb"-?[0-9]+\Z")
_KNOWN_PREFIXES = (
    b"NXRS_BLE_WITNESS_READY",
    b"NXRS_BLE_WITNESS_SEEN",
    b"NXRS_BLE_WITNESS_DONE",
    b"NXRS_BLE_SCAN_FAIL",
)


@dataclass(frozen=True)
class WitnessResult:
    matches: int
    limited: bool
    seen_at: tuple[float, ...]


class _ConsoleReader:
    """Read bounded lines and recognize a prompt that has no trailing LF."""

    def __init__(self, console, deadline, clock):
        self.console = console
        self.deadline = deadline
        self.clock = clock

    def line_or_prompt(self):
        line = bytearray()
        raw_size = 0
        in_csi = False
        esc_pending = False
        while self.clock() < self.deadline:
            try:
                chunk = self.console.read(1)
            except Exception:
                raise RuntimeError("BLE witness console read failed") from None
            if not chunk:
                continue
            byte = chunk[0]
            raw_size += 1
            if raw_size > _MAX_LINE:
                raise RuntimeError("BLE witness console line exceeded limit")

            if in_csi:
                if 0x40 <= byte <= 0x7e:
                    in_csi = False
                continue
            if esc_pending:
                esc_pending = False
                if byte == ord("["):
                    in_csi = True
                    continue
                line.append(0x1b)
            if byte == 0x1b:
                esc_pending = True
                continue
            if byte == 0x0a:
                if line.endswith(b"\r"):
                    del line[-1:]
                return bytes(line), False
            line.append(byte)
            if line == _PROMPT:
                return b"", True
            if len(line) > _MAX_LINE:
                raise RuntimeError("BLE witness console line exceeded limit")
        raise RuntimeError("BLE witness timed out")


def _known_record(line):
    return any(line.startswith(prefix) for prefix in _KNOWN_PREFIXES)


def _parse_elapsed(match):
    return int(match.group(1))


def read_witness(console, *, timeout=25, clock=time.monotonic):
    """Run and validate one BLE scan witness exchange on an open serial console.

    The caller owns the console and any reader thread. The supplied console is
    expected to have a short read timeout (normally 0.1 seconds).
    """
    if timeout <= 0:
        raise ValueError("BLE witness timeout must be positive")
    deadline = clock() + timeout
    reader = _ConsoleReader(console, deadline, clock)
    try:
        console.write(b"ble_test fixture\n")
    except Exception:
        raise RuntimeError("BLE witness console write failed") from None

    ready_elapsed = None
    previous_elapsed = None
    seen_at = []
    done = None
    while done is None:
        line, prompt = reader.line_or_prompt()
        if prompt:
            raise RuntimeError("BLE witness prompt arrived before completion")
        if line.startswith(b"NXRS_BLE_SCAN_FAIL"):
            raise RuntimeError("BLE scan failed")

        ready = _READY.fullmatch(line)
        seen = _SEEN.fullmatch(line)
        finished = _DONE.fullmatch(line)
        if ready:
            if ready_elapsed is not None or seen_at:
                raise RuntimeError("duplicate or out-of-order BLE witness READY")
            ready_elapsed = _parse_elapsed(ready)
            previous_elapsed = ready_elapsed
        elif seen:
            if ready_elapsed is None:
                raise RuntimeError("BLE witness SEEN arrived before READY")
            elapsed = _parse_elapsed(seen)
            if elapsed < previous_elapsed:
                raise RuntimeError("BLE witness elapsed time moved backwards")
            previous_elapsed = elapsed
            seen_at.append(clock())
            if len(seen_at) > _MAX_MATCHES:
                raise RuntimeError("BLE witness exceeded match limit")
        elif finished:
            if ready_elapsed is None:
                raise RuntimeError("BLE witness DONE arrived before READY")
            matches = int(finished.group(1))
            if matches > _MAX_MATCHES or matches != len(seen_at):
                raise RuntimeError("BLE witness match count did not agree")
            done = (matches, finished.group(2) == b"true")
        elif _known_record(line):
            raise RuntimeError("malformed BLE witness record")

    # A completion record is not success until NSH reports clean shutdown.
    line, prompt = reader.line_or_prompt()
    while not prompt and not _known_record(line):
        line, prompt = reader.line_or_prompt()
    if not prompt:
        if line.startswith(b"NXRS_BLE_SCAN_FAIL"):
            raise RuntimeError("BLE scan failed")
        raise RuntimeError("unexpected BLE witness record after DONE")

    try:
        console.write(b"echo $?\n")
    except Exception:
        raise RuntimeError("BLE witness console write failed") from None

    status = None
    while status is None:
        line, prompt = reader.line_or_prompt()
        if prompt:
            raise RuntimeError("BLE witness exit status was missing")
        if _STATUS.fullmatch(line):
            value = int(line)
            if value != 0:
                raise RuntimeError("BLE witness exit status was not zero")
            status = value
        elif line.startswith(b"NXRS_BLE_SCAN_FAIL"):
            raise RuntimeError("BLE scan failed")
        elif _known_record(line):
            raise RuntimeError("malformed BLE witness record")

    line, prompt = reader.line_or_prompt()
    while not prompt:
        if _STATUS.fullmatch(line) or _known_record(line):
            raise RuntimeError("extra output after BLE witness exit status")
        line, prompt = reader.line_or_prompt()

    return WitnessResult(done[0], done[1], tuple(seen_at))
