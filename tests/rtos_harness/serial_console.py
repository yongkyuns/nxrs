"""Bounded NSH transport for an already running physical image.

This helper only opens a 115200-baud console and exchanges shell text. It does
not flash, reset, toggle modem lines, or request the 1200-baud bootloader. The
serial factory and monotonic clock are injectable for hardware-free tests.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import secrets
import time


_PROMPT = rb"(?:\A|(?<=\n))nsh>[ \t]*"
_READ_BYTES = 4096
_IO_SLICE = 0.1


def _serial_factory():
    try:
        import serial
    except ImportError as error:
        raise RuntimeError("physical serial requires pyserial==3.5") from error
    if getattr(serial, "__version__", None) != "3.5":
        raise RuntimeError("physical serial requires pyserial==3.5")
    return serial.Serial()


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError("serial timeout must be finite and positive")
    return float(value)


class SerialConsole:
    """One synchronous console; responses retain echo, CR/LF and the NSH prompt.

    Call synchronize before qualification commands (shell also does this when
    needed). Every received byte is retained in raw, up to the transcript cap,
    and written to log on close, including failures. Timeouts or transport and
    framing failures close the connection: a partially sent command is never
    silently retried by a later call.
    """

    def __init__(self, port, log: Path, *, timeout=5.0,
                 max_response_bytes=65536, max_transcript_bytes=1024 * 1024,
                 serial_factory=None, clock=None):
        self.port = port
        self.log = Path(log)
        self.timeout = _timeout(timeout)
        for value in (max_response_bytes, max_transcript_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("serial byte limits must be positive integers")
        self.max_response_bytes = max_response_bytes
        self.max_transcript_bytes = max_transcript_bytes
        self._factory = _serial_factory if serial_factory is None else serial_factory
        self._clock = time.monotonic if clock is None else clock
        self._connection = None
        self._raw = bytearray()
        self._pending = bytearray()
        self._synchronized = False
        self.closed = False

    @property
    def raw(self) -> bytes:
        return bytes(self._raw)

    def _open(self):
        if self.closed:
            raise RuntimeError("serial console is closed")
        if self._connection is not None:
            return
        # Like the radio runner, construct without a port so no default modem
        # line state is applied by opening before DTR and RTS are configured.
        connection = self._factory()
        self._connection = connection
        if connection.is_open:
            raise RuntimeError("serial factory must return an unopened connection")
        connection.port = self.port
        connection.baudrate = 115200
        connection.timeout = min(_IO_SLICE, self.timeout)
        connection.write_timeout = min(_IO_SLICE, self.timeout)
        connection.dtr = False
        connection.rts = False
        connection.open()

    def __enter__(self):
        try:
            self._open()
            return self
        except BaseException as error:
            self._close_after_error(error)
            raise

    def __exit__(self, error_type, error, traceback):
        if error is None:
            self.close()
        else:
            self._close_after_error(error)
        return False

    def _close_after_error(self, error):
        try:
            self.close()
        except Exception as cleanup:
            error.add_note(f"serial close/log failed: {cleanup}")

    def close(self):
        """Close once and save the received transcript even if close fails."""
        if self.closed:
            return
        self.closed = True
        connection, self._connection = self._connection, None
        try:
            if connection is not None:
                connection.close()
        finally:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            self.log.write_bytes(self._raw)

    def _remaining(self, deadline, operation):
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError(f"serial deadline {operation}")
        return remaining

    def _write(self, data, deadline):
        offset = 0
        while offset < len(data):
            remaining = self._remaining(deadline, "writing command")
            if not self._connection.is_open:
                raise RuntimeError("serial disconnected while writing command")
            self._connection.write_timeout = min(_IO_SLICE, remaining)
            try:
                written = self._connection.write(data[offset:offset + _READ_BYTES])
            except OSError as error:
                raise RuntimeError("serial command write failed") from error
            if not isinstance(written, int) or not 0 < written <= min(
                    _READ_BYTES, len(data) - offset):
                raise RuntimeError("serial command write made no valid progress")
            offset += written
        # Do not use flush/tcdrain: the write timeout does not bound those calls.

    def _until(self, pattern, deadline):
        pattern = re.compile(pattern)
        while True:
            match = pattern.search(self._pending)
            if match is not None and match.end() <= self.max_response_bytes:
                response = bytes(self._pending[:match.end()])
                del self._pending[:match.end()]
                return response
            if len(self._pending) > self.max_response_bytes:
                raise RuntimeError("serial response limit exceeded")
            remaining = self._remaining(deadline, "waiting for NSH response")
            if not self._connection.is_open:
                raise RuntimeError("serial disconnected before NSH response")
            self._connection.timeout = min(_IO_SLICE, remaining)
            # One byte beyond each remaining budget detects overflow while
            # bounding both the read and retained diagnostic bytes.
            amount = min(_READ_BYTES,
                         self.max_response_bytes - len(self._pending) + 1,
                         self.max_transcript_bytes - len(self._raw) + 1)
            try:
                data = self._connection.read(amount)
            except OSError as error:
                raise RuntimeError("serial disconnected before NSH response") from error
            available = self.max_transcript_bytes - len(self._raw)
            self._raw.extend(data[:available])
            if len(data) > available:
                raise RuntimeError("serial transcript limit exceeded")
            self._pending.extend(data)

    def synchronize(self):
        """Consume old/handshake prompts before permitting a fresh command.

        Three newlines wake the existing shell. A fresh, harmless echo then
        orders all resulting prompts before its standalone nonce response.
        The command echo alone cannot satisfy that response; no quiet-period
        guess or destructive input-buffer reset is needed.
        """
        try:
            self._open()
            if self._synchronized:
                return
            deadline = self._clock() + self.timeout
            self._write(b"\n\n\n", deadline)
            self._until(_PROMPT, deadline)
            nonce = "NXRS_CONSOLE_SYNC_" + secrets.token_hex(12)
            self._write(f"echo {nonce}\n".encode("ascii"), deadline)
            self._until(rb"(?:\A|(?<=\n))" + nonce.encode("ascii")
                        + rb"\r*\n" + _PROMPT, deadline)
            self._synchronized = True
        except BaseException as error:
            self._close_after_error(error)
            raise

    def shell(self, command: str, timeout=None) -> str:
        """Return one complete response, including any echo, through nsh>."""
        duration = self.timeout if timeout is None else _timeout(timeout)
        if not isinstance(command, str) or not command.strip() \
                or any(char in command for char in ("\r", "\n", "\0")):
            raise ValueError("shell requests must be single nonempty commands")
        encoded = command.encode("utf-8") + b"\n"
        if len(encoded) > self.max_response_bytes:
            raise ValueError("serial command exceeds the response byte limit")
        try:
            self.synchronize()
            deadline = self._clock() + duration
            self._write(encoded, deadline)
            return self._until(_PROMPT, deadline).decode("utf-8", errors="replace")
        except BaseException as error:
            self._close_after_error(error)
            raise
