"""Bounded Unix process transport for the same GATT qualification records.

No synthetic NSH prompt or shell status: EOF and the actual exit code must both
confirm completion. Only the child process group created here is ever stopped.
"""
import os
import selectors
import signal
import subprocess
import threading
import time

MAX_LINE = 8192
MAX_OUTPUT = 128 * 1024


class RunError(RuntimeError):
    """A static explanation, never raw SDK output, path or device identity."""


def _group_exists(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can report this while an exiting leader awaits reaping.
        # Unknown is not a stopped group; poll/reap and verify again.
        return True


def _stop(process):
    # Descendants can retain the pipe even after the group leader exits.
    process.poll()
    if not _group_exists(process.pid):
        process.wait(timeout=2)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # EPERM can be an exiting, not-yet-reaped Darwin leader. Reap and
        # verify the group; never turn this observation into a false barrier.
        pass
    deadline = time.monotonic() + 2
    while _group_exists(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.01)
    if _group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise RunError('native client process group did not stop') from None
    deadline = time.monotonic() + 2
    while _group_exists(process.pid):
        if time.monotonic() >= deadline:
            raise RunError('native client process group did not stop')
        time.sleep(0.01)


class ProcessClient:
    def __init__(self, command):
        self.command = list(command)
        self._process = None
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def close(self):
        self._closed.set()
        with self._lock:
            process = self._process
        if process is not None:
            _stop(process)

    def exchange(self, case, records, *, timeout=95, clock=time.monotonic):
        if os.name != 'posix':
            raise RunError('native client requires a Unix host')
        if self._closed.is_set():
            raise RunError('native client closed')
        arguments = self.command + ([] if case == 'full' else [case])
        process = None
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       bufsize=0, start_new_session=True)
            with self._lock:
                self._process = process
            deadline = clock() + timeout
            buffer = bytearray()
            received = 0
            eof = False
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                while not (eof and process.poll() is not None):
                    if self._closed.is_set():
                        raise RunError('native client cancelled')
                    remaining = deadline - clock()
                    if remaining <= 0:
                        raise RunError('native client timed out')
                    for key, _ in ready.select(min(remaining, 0.1)):
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            eof = True
                            ready.unregister(key.fileobj)
                            continue
                        received += len(chunk)
                        if received > MAX_OUTPUT:
                            raise RunError('native client output exceeded limit')
                        buffer.extend(chunk)
                        while b'\n' in buffer:
                            line, _, rest = buffer.partition(b'\n')
                            buffer = bytearray(rest)
                            if len(line) > MAX_LINE:
                                raise RunError('native client line exceeded limit')
                            records.observe(bytes(line).removesuffix(b'\r'))
                        if len(buffer) > MAX_LINE:
                            raise RunError('native client line exceeded limit')
            if buffer:
                raise RunError('native client output ended mid-line')
            if process.wait(timeout=max(0.01, deadline - clock())) != 0:
                raise RunError('native client exited nonzero')
            records.finish()
        except (OSError, subprocess.SubprocessError):
            raise RunError('native client process failed') from None
        finally:
            if process is not None:
                stopped = False
                try:
                    _stop(process)
                    stopped = True
                finally:
                    process.stdout.close()
                    if stopped:
                        with self._lock:
                            if self._process is process:
                                self._process = None
