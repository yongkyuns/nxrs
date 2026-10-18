#!/usr/bin/env python3
"""Strict GATT qualification: device or native client against a device peer."""

import argparse
import importlib.util
import queue
import re
import secrets
import sys
import threading
import time
from pathlib import Path


CLIENT_LIMIT = 95.0
READY_LIMIT = 5.0
PEER_LIMIT = 95.0
STATUS_LIMIT = 5.0
JOIN_LIMIT = 2.0
_CLIENT_FINISHED = object()
_FAILURE_STAGES = frozenset((
    b"remote_error_contract", b"long_value_mismatch", b"oversized_value_not_rejected",
    b"oversized_value_not_quiescent", b"cancel_outcome", b"cancel_not_quiescent",
    b"recovery_timeout", b"peer_missing", b"peer_ambiguous",
    b"gatt_pending", b"gatt_backend_busy", b"gatt_invalid", b"gatt_unavailable",
    b"gatt_admission_timeout",
    b"operation_unsupported", b"operation_busy", b"operation_unavailable",
    b"operation_denied", b"operation_invalid", b"operation_timeout", b"operation_io",
    b"link_lost", b"cancelled", b"att_error", b"protocol_error",
))


class ProtocolError(RuntimeError):
    """A static protocol explanation, never raw console/driver error text."""


_MODES = {
    "full": {
        "client": "gatt_client\n",
        "peer": "radio_test ble-only\n",
        "phases": ("start", "scan_started", "scan_stopped", "connected", "discovered", "disconnected"),
        "pass": "NXRS_GATT_CLIENT result=pass pairs=100 bytes_per_pair=20 cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed next=rerun_after_fixture_restart",
        "values": (),
        "peer_ready": b"NXRS_RADIO_GATT_READY rounds=100",
        "peer_done": b"NXRS_RADIO_GATT_DONE",
    },
    "cancel": {
        "client": "gatt_client cancel\n",
        "peer": "radio_test ble-cancel\n",
        "phases": ("start", "scan_started", "scan_stopped", "connected", "cancel_requested", "recovered"),
        "pass": "NXRS_GATT_CLIENT result=pass mode=cancel cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed",
        "values": (),
        "peer_ready": b"NXRS_RADIO_GATT_READY mode=cancel",
        "peer_done": None,
    },
    "values": {
        "client": "gatt_client values\n",
        "peer": "radio_test ble-values\n",
        "phases": ("start", "scan_started", "scan_stopped", "connected", "discovered", "disconnected"),
        "pass": "NXRS_GATT_CLIENT result=pass mode=values pairs=100 bytes_per_pair=20 complete_values=44,64 oversized=65_rejected remote_error=preserved cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed",
        "values": (
            b"NXRS_GATT_VALUES check=remote_error att=14 link=preserved",
            b"NXRS_GATT_VALUES check=complete_read bytes=44",
            b"NXRS_GATT_VALUES check=complete_read bytes=64",
            b"NXRS_GATT_VALUES check=oversized_read bytes=65 capacity=64 outcome=rejected barrier=quiescent",
        ),
        "peer_ready": b"NXRS_RADIO_GATT_READY rounds=100",
        "peer_done": b"NXRS_RADIO_GATT_DONE",
    },
}
# Compatibility runs use the same exact echo/lifecycle contract, but select
# the immutable V1 service. Keep their pass record distinct from V2 evidence.
_MODES["legacy"] = {
    **_MODES["full"],
    "client": "gatt_client legacy\n",
    "pass": "NXRS_GATT_CLIENT result=pass mode=legacy pairs=100 bytes_per_pair=20 cleanup_gatt=confirmed cleanup_scan=confirmed native_close=confirmed",
}
_PEER_PASS = b"NXRS_RADIO_PASS wifi=not-started worker=not-started"
_CANCEL_PASS = b"NXRS_RADIO_CANCEL_PASS connected=1 disconnected=1 writes=0 reads=0 invalid=0 cleanup=confirmed"
_STATUS = re.compile(rb"[0-9]+\Z")
_RADIO_RECORD = re.compile(rb"NXRS_RADIO_[A-Z0-9_]+(?: .*)?\Z")
_PHASE = re.compile(rb"NXRS_GATT_CLIENT phase=([^ ]+)(?: .*)?\Z")
_RESULT = re.compile(rb"NXRS_GATT_CLIENT result=([^ ]+)(?: .*)?\Z")


class _ClientRecords:
    """Validate the ordered GATT client records independently of the transport."""
    def __init__(self, case):
        self.protocol = _MODES[case]
        self.phase_index = 0
        self.value_index = 0
        self.passed = False

    def observe(self, line):
        if line.startswith(b"NXRS_GATT_CLIENT"):
            phase_match = _PHASE.fullmatch(line)
            result_match = _RESULT.fullmatch(line)
            if phase_match:
                expected = self.protocol["phases"]
                phase = phase_match.group(1).decode("ascii", "replace")
                if (self.passed or self.phase_index >= len(expected)
                        or phase != expected[self.phase_index]):
                    raise ProtocolError("client phase order invalid")
                self.phase_index += 1
                return
            if result_match:
                result = result_match.group(1)
                if result == b"fail":
                    stage = re.search(rb" stage=([a-z_]+)(?: |$)", line)
                    if stage is not None and stage[1] in _FAILURE_STAGES:
                        raise ProtocolError("client failed: " + stage[1].decode("ascii"))
                    raise ProtocolError("client reported failure")
                if (self.passed or self.phase_index != len(self.protocol["phases"])
                        or line.decode("ascii", "replace") != self.protocol["pass"]):
                    raise ProtocolError("client result record invalid")
                if self.value_index != len(self.protocol["values"]):
                    raise ProtocolError("client values records incomplete")
                self.passed = True
                return
            raise ProtocolError("client protocol record malformed")
        if line.startswith(b"NXRS_GATT_VALUES"):
            values = self.protocol["values"]
            if (self.passed or self.phase_index != 5 or self.value_index >= len(values)
                    or line != values[self.value_index]):
                raise ProtocolError("client values record invalid")
            self.value_index += 1

    def finish(self):
        if not self.passed:
            raise ProtocolError("client ended before exact pass record")


def _reader_type():
    """Load the shared bounded ANSI-stripping parser without copying it."""
    path = Path(__file__).resolve().parents[1] / "radio" / "witness.py"
    spec = importlib.util.spec_from_file_location("nxrs_radio_witness", path)
    if spec is None or spec.loader is None:
        raise ProtocolError("shared console parser unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._ConsoleReader


_ConsoleReader = _reader_type()


def open_serial(port, serial_module=None):
    """Open a pyserial console with reset/flow-control lines disabled."""
    if serial_module is None:
        import serial as serial_module
    console = serial_module.Serial(
        port=None, baudrate=115200, timeout=0.1, write_timeout=1,
        exclusive=True,
    )
    console.dtr = False
    console.rts = False
    console.port = port
    console.open()
    return console


def _write(console, data):
    try:
        console.write(data)
    except Exception:
        raise ProtocolError("console write failed") from None


def sync_console(console, label, timeout=5.0, token_factory=None, clock=time.monotonic):
    """Drain stale console output behind a fresh echoed marker and prompt."""
    token_factory = token_factory or (lambda: secrets.token_hex(12))
    marker = f"NXRS_SYNC_{token_factory()}".encode("ascii")
    _write(console, b"\n\n\n")
    _write(console, b"echo " + marker + b"\n")
    reader = _ConsoleReader(console, clock() + timeout, clock)
    seen = False
    while True:
        line, prompt = reader.line_or_prompt()
        if not seen:
            if line == marker:
                seen = True
            continue
        if prompt:
            return
        raise ProtocolError(f"{label} sync marker was not followed by a prompt")


def _status(console, label, clock=time.monotonic, *, observe=None):
    _write(console, b"echo $?\n")
    deadline = clock() + STATUS_LIMIT
    reader = _ConsoleReader(console, deadline, clock)
    echoed = False
    status = None
    while True:
        try:
            line, prompt = reader.line_or_prompt()
        except RuntimeError:
            raise ProtocolError(f"{label} shell status missing or nonzero") from None
        if observe is not None:
            observe(line, prompt)
        if prompt:
            if echoed and status == b"0":
                return
            raise ProtocolError(f"{label} shell status missing or nonzero")
        if line.startswith((b"NXRS_GATT_CLIENT", b"NXRS_GATT_VALUES", b"NXRS_RADIO_")):
            raise ProtocolError(f"{label} shell status protocol marker duplicated or malformed")
        if not echoed:
            if line == b"echo $?":
                echoed = True
            continue
        if line == b"echo $?":
            raise ProtocolError(f"{label} shell echoed status command more than once")
        if status is None:
            if not _STATUS.fullmatch(line):
                raise ProtocolError(f"{label} shell status malformed")
            status = line
        elif _STATUS.fullmatch(line):
            raise ProtocolError(f"{label} shell returned multiple statuses")


def _client_exchange(console, case, clock=time.monotonic, *, command=None,
                     observe=None, validate=None, client_limit=None):
    """Keep the GATT oracle intact when composed with another fixture."""
    protocol = _MODES[case]
    _write(console, (command or protocol["client"]).encode("ascii"))
    deadline = clock() + (CLIENT_LIMIT if client_limit is None else client_limit)
    reader = _ConsoleReader(console, deadline, clock)
    records = _ClientRecords(case)
    while True:
        line, prompt = reader.line_or_prompt()
        if observe is not None:
            observe(line, prompt)
        if prompt:
            records.finish()
            if validate is not None:
                validate()
            break
        records.observe(line)
    _status(console, "client", clock, observe=observe)


def _peer_record(line):
    return line.startswith((b"NXRS_RADIO_NATIVE_FAIL", b"NXRS_RADIO_FAIL"))


class _PeerPump:
    """Bounded reader thread feeding complete peer console records."""
    def __init__(self, console, deadline, clock=time.monotonic):
        self.console = console
        self.deadline = deadline
        self.clock = clock
        self.events = queue.Queue(maxsize=32)
        self.overflow = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="gatt-peer-reader", daemon=True)

    def _publish(self, item):
        if item[1] is False:
            line = item[0]
            relevant = (
                _RADIO_RECORD.fullmatch(line) is not None
                or line == b"echo $?"
                or line == b"console protocol read failed"
                or _STATUS.fullmatch(line) is not None
            )
        else:
            relevant = True
        if not relevant:
            return
        self._enqueue(item)

    def _enqueue(self, item):
        try:
            self.events.put_nowait(item)
        except queue.Full:
            self.overflow.set()

    def _run(self):
        while not self.stop.is_set() and self.clock() < self.deadline:
            reader = _ConsoleReader(self.console, self.deadline, self.clock)
            try:
                item = reader.line_or_prompt()
                self._publish(item)
            except RuntimeError as error:
                self._publish((b"console protocol read failed", False))
                return

    def get(self, deadline):
        if self.overflow.is_set():
            raise ProtocolError("peer protocol queue overflow")
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ProtocolError("peer protocol timed out")
        try:
            item = self.events.get(timeout=remaining)
        except queue.Empty:
            raise ProtocolError("peer protocol timed out") from None
        if self.overflow.is_set():
            raise ProtocolError("peer protocol queue overflow")
        if item == (b"console protocol read failed", False):
            raise ProtocolError("console protocol read failed")
        return item

    def close(self):
        self.stop.set()
        try:
            self.console.close()
        except Exception:
            pass
        if self.thread.ident is None:
            return True
        self.thread.join(JOIN_LIMIT)
        return not self.thread.is_alive()


def _peer_until(pump, predicate, deadline, label):
    while True:
        line, prompt = pump.get(deadline)
        if prompt:
            raise ProtocolError(f"{label} prompt arrived early")
        if _peer_record(line):
            raise ProtocolError("peer reported native or radio failure")
        if predicate(line):
            return line


def _peer_status(peer, pump, clock=time.monotonic, case_deadline=None):
    _write(peer, b"echo $?\n")
    deadline = clock() + STATUS_LIMIT
    if case_deadline is not None:
        deadline = min(deadline, case_deadline)
    echoed = False
    status = None
    while True:
        line, prompt = pump.get(deadline)
        if prompt:
            if echoed and status == b"0":
                return
            raise ProtocolError("peer shell status missing or nonzero")
        if line.startswith(b"NXRS_RADIO_"):
            raise ProtocolError("peer shell status protocol marker duplicated or malformed")
        if not echoed:
            if line == b"echo $?":
                echoed = True
            continue
        if line == b"echo $?":
            raise ProtocolError("peer shell echoed status command more than once")
        if status is None:
            if not _STATUS.fullmatch(line):
                raise ProtocolError("peer shell status malformed")
            status = line
        elif _STATUS.fullmatch(line):
            raise ProtocolError("peer shell returned multiple statuses")


class _PeerState:
    """Retain peer progress independently of the client's cleanup timing."""
    def __init__(self, case):
        self.cancel = case == "cancel"
        self.done = self.passed = self.prompt = self.finish_sent = False

    def observe(self, line, prompt):
        if prompt:
            if not self.passed or self.prompt:
                raise ProtocolError("peer prompt arrived before cleanup proof")
            self.prompt = True
        elif _peer_record(line):
            raise ProtocolError("peer reported native or radio failure")
        elif line.startswith(b"NXRS_RADIO_GATT_READY"):
            raise ProtocolError("peer ready record duplicated")
        elif line.startswith(b"NXRS_RADIO_GATT_DONE"):
            if self.cancel or self.done or line != b"NXRS_RADIO_GATT_DONE":
                raise ProtocolError("peer done record invalid")
            self.done = True
        elif line.startswith((b"NXRS_RADIO_PASS", b"NXRS_RADIO_CANCEL_PASS")):
            expected = _CANCEL_PASS if self.cancel else _PEER_PASS
            if self.passed or line != expected or (not self.cancel and not self.finish_sent):
                raise ProtocolError("peer cleanup proof invalid")
            self.passed = True


def _run_case(client, peer, pump, case, clock=time.monotonic, *,
              exchange=None, client_limit=None):
    protocol = _MODES[case]
    _write(peer, protocol["peer"].encode("ascii"))
    ready_deadline = clock() + READY_LIMIT
    ready = _peer_until(pump, lambda line: line.startswith(protocol["peer_ready"]), ready_deadline, "peer ready")
    if ready != protocol["peer_ready"]:
        raise ProtocolError("peer ready record invalid")
    case_deadline = clock() + (CLIENT_LIMIT if client_limit is None else client_limit)

    client_result = []
    client_error = []
    def run_client():
        try:
            (exchange or _client_exchange)(client, case, clock)
            client_result.append(True)
        except Exception as error:
            client_error.append(error if isinstance(error, ProtocolError)
                                else ProtocolError("client protocol failed"))
        finally:
            # Both serial progress and client completion use one bounded wait
            # queue. The sentinel cannot be forged by an incoming text line.
            pump._enqueue(_CLIENT_FINISHED)

    worker = threading.Thread(target=run_client, name="gatt-client-reader", daemon=True)
    state = _PeerState(case)
    client_finished = False
    worker.start()
    try:
        while not (client_finished and state.prompt):
            event = pump.get(case_deadline)
            if event is _CLIENT_FINISHED:
                worker.join(JOIN_LIMIT)
                if worker.is_alive():
                    raise ProtocolError("client reader thread did not finish after close")
                if client_error:
                    raise client_error[0]
                if not client_result:
                    raise ProtocolError("client protocol failed")
                client_finished = True
            else:
                state.observe(*event)
            if client_finished and state.done and not state.finish_sent:
                # GATT_DONE is allowed before native client release. Wait for
                # both proofs, then authorize peripheral teardown exactly once.
                _write(peer, b"done\n")
                state.finish_sent = True
        _peer_status(peer, pump, clock, case_deadline)
    finally:
        if worker.is_alive():
            try:
                client.close()
            except Exception:
                pass
            worker.join(JOIN_LIMIT)
            if worker.is_alive():
                raise ProtocolError("client reader thread did not finish after close") from None
        else:
            worker.join(0)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    client = parser.add_mutually_exclusive_group(required=True)
    client.add_argument("--client-port")
    client.add_argument("--native-client", type=Path,
                        help="built native gatt-client executable (Unix)")
    parser.add_argument("--client-app", choices=("gatt_client", "radio_test"),
                        help="serial firmware entry (default: gatt_client); radio_test uses ble-client")
    parser.add_argument("--peer-port", required=True)
    parser.add_argument("--case", action="append", choices=tuple(_MODES), default=None)
    return parser


def qualify(client_port, peer_port, cases=None, serial_module=None, *,
            exchange=None, client_limit=None, native_client=None):
    """Share serial ownership and checked teardown with composed scenarios."""
    cases = cases or ["full"]
    limit = CLIENT_LIMIT if client_limit is None else client_limit
    native = native_client is not None
    if native:
        try:
            if client_port is not None or exchange is not None:
                raise ValueError('ambiguous client selection')
            binary = Path(native_client).resolve(strict=True)
            if not binary.is_file():
                raise ValueError('native client is not a file')
            spec = importlib.util.spec_from_file_location("nxrs_gatt_native", Path(__file__).with_name("native.py"))
            transport = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transport)
            client = transport.ProcessClient([str(binary)])

            def exchange(client, case, clock):
                try:
                    client.exchange(case, _ClientRecords(case), timeout=limit, clock=clock)
                except transport.RunError as error:
                    raise ProtocolError(str(error)) from None
        except Exception:
            print("FAIL: native client unavailable")
            print("FAIL cases=0 status=1")
            return 1
    try:
        if not native:
            client = open_serial(client_port, serial_module)
        try:
            peer = open_serial(peer_port, serial_module)
        except Exception:
            client.close()
            raise
    except Exception:
        print("FAIL: serial open failed")
        print("FAIL cases=0 status=1")
        return 1
    success = 0
    pump = None
    peer_thread_stuck = False
    try:
        try:
            if not native:
                sync_console(client, "client")
            sync_console(peer, "peer")
            pump = _PeerPump(peer, time.monotonic() + len(cases) * (limit + PEER_LIMIT + 30.0))
            pump.thread.start()
        except Exception:
            print("FAIL: console sync failed")
            print("FAIL cases=0 status=1")
            return 1
        for case in cases:
            try:
                if exchange is None and client_limit is None:
                    _run_case(client, peer, pump, case)
                else:
                    _run_case(client, peer, pump, case, exchange=exchange, client_limit=limit)
            except Exception as error:
                safe = str(error) if isinstance(error, ProtocolError) else "case protocol failed"
                print(f"FAIL case={case} code={safe.replace(' ', '_')}", flush=True)
                print(f"FAIL cases={success} status=1", flush=True)
                return 1
            success += 1
            print(f"PASS case={case} index={success}", flush=True)
    finally:
        if pump is not None:
            if not pump.close():
                print("FAIL code=peer_reader_thread_stuck", flush=True)
                peer_thread_stuck = True
        for console in (client, peer):
            try:
                console.close()
            except Exception:
                pass
    if peer_thread_stuck:
        print(f"FAIL cases={success} status=1", flush=True)
        return 1
    print(f"PASS cases={success} status=0", flush=True)
    return 0


def main(argv=None, serial_module=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.client_app is not None and args.client_port is None:
        parser.error("--client-app requires --client-port")
    exchange = None
    if args.client_app == "radio_test":
        def exchange(console, case, clock):
            suffix = "" if case == "full" else " " + case
            return _client_exchange(console, case, clock,
                                    command="radio_test ble-client" + suffix + "\n")
    return qualify(args.client_port, args.peer_port, args.case, serial_module,
                   native_client=args.native_client, exchange=exchange)


if __name__ == "__main__":
    raise SystemExit(main())
