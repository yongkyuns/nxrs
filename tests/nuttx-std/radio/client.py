"""Compose portable two-board GATT qualification with the existing echo peer.

The BLE runner still owns phase ordering, shell statuses and serial teardown.
This layer only supplies credentials and checks same-board network overlap.
"""

import importlib.util
from pathlib import Path
import re


def _load_gatt():
    path = Path(__file__).resolve().parents[1] / "ble/client.py"
    spec = importlib.util.spec_from_file_location("nxrs_gatt_client_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATT = _load_gatt()
# Association/traffic cleanup are outside the unchanged 50-second GATT workload.
CLIENT_LIMIT = 140.0
_OVERLAP = re.compile(
    rb"NXRS_RADIO_CLIENT_OVERLAP first_ms=([0-9]+) last_ms=([0-9]+) "
    rb"first_echoes=([0-9]+) last_echoes=([0-9]+)\Z"
)
_TRAFFIC = re.compile(rb"NXRS_RADIO_TRAFFIC_PASS gatt=100 (udp|tcp)=([0-9]+)\Z")
_PASS = b"NXRS_RADIO_PASS wifi=disconnected worker=joined"


class Evidence:
    """Ordered device proof; never retain or report raw console records."""

    def __init__(self, console, ssid, password, transport):
        self.console, self.ssid, self.password = console, ssid, password
        self.transport = transport.encode("ascii")
        self.ready = self.associated = self.started = self.gatt_pass = False
        self.overlap = None
        self.count = None
        self.passed = False

    def observe(self, line, prompt):
        if self.password and self.password in line:
            raise GATT.ProtocolError("credential echo detected")
        if prompt:
            return
        if line.startswith((b"NXRS_RADIO_FAIL", b"NXRS_RADIO_NATIVE_FAIL",
                            b"NXRS_WIFI_STATION_FAIL", b"NXRS_WIFI_TRAFFIC_FAIL")):
            raise GATT.ProtocolError("device radio failure")
        if line.startswith(b"NXRS_WIFI_CREDENTIALS_READY"):
            if self.ready or line != b"NXRS_WIFI_CREDENTIALS_READY":
                raise GATT.ProtocolError("credentials readiness invalid")
            self.ready = True
            GATT._write(self.console, bytes((len(self.ssid), len(self.password)))
                        + self.ssid + self.password)
        elif line.startswith(b"NXRS_WIFI_ASSOCIATED_DHCP"):
            if not self.ready or self.associated or line != b"NXRS_WIFI_ASSOCIATED_DHCP":
                raise GATT.ProtocolError("association proof invalid")
            self.associated = True
        elif line.startswith(b"NXRS_GATT_CLIENT phase="):
            if not self.associated or self.gatt_pass:
                raise GATT.ProtocolError("GATT outside association lifetime")
            self.started = True
        elif line.startswith(b"NXRS_GATT_CLIENT result=pass"):
            if not self.started or self.gatt_pass:
                raise GATT.ProtocolError("GATT completion invalid")
            self.gatt_pass = True
        elif line.startswith(b"NXRS_RADIO_CLIENT_OVERLAP"):
            match = _OVERLAP.fullmatch(line)
            if not self.gatt_pass or self.overlap is not None or not match:
                raise GATT.ProtocolError("overlap proof invalid")
            first_ms, last_ms, first, last = map(int, match.groups())
            if last_ms < first_ms or first != 0 or last - first < 6:
                raise GATT.ProtocolError("network did not overlap GATT data")
            self.overlap = last
        elif line.startswith(b"NXRS_RADIO_TRAFFIC_PASS"):
            match = _TRAFFIC.fullmatch(line)
            if (self.overlap is None or self.count is not None or not match
                    or match[1] != self.transport or int(match[2]) < self.overlap):
                raise GATT.ProtocolError("traffic proof invalid")
            self.count = int(match[2])
        elif line.startswith(b"NXRS_RADIO_PASS"):
            if self.count is None or self.passed or line != _PASS:
                raise GATT.ProtocolError("radio cleanup proof invalid")
            self.passed = True

    def validate(self):
        if not self.passed:
            raise GATT.ProtocolError("radio evidence incomplete")


def qualify(client_port, peer_port, host_ip, ssid, password, transport, *,
            echo_type, serial_module=None):
    """One bounded attempt; no host Bluetooth adapter, reflash or retry."""
    if transport not in ("udp", "tcp") or not 1 <= len(ssid) <= 32:
        raise ValueError("invalid radio client configuration")
    if len(password) > 63 or (password and len(password) < 8):
        raise ValueError("invalid credential length")

    def exchange(console, case, clock):
        with echo_type(host_ip, transport) as echo:
            evidence = Evidence(console, ssid, password, transport)
            GATT._client_exchange(
                console, case, clock,
                command=f"radio_test {host_ip}:{echo.port} {transport} portable-client\n",
                observe=evidence.observe, validate=evidence.validate,
                client_limit=CLIENT_LIMIT,
            )
        # Both device and host workers must be joined before the BLE runner is
        # allowed to authorize peripheral teardown or print a case PASS.
        try:
            echo.validate(expected_count=evidence.count, minimum=6)
        except RuntimeError:
            raise GATT.ProtocolError("host echo evidence invalid") from None

    return GATT.qualify(client_port, peer_port, serial_module=serial_module,
                        exchange=exchange, client_limit=CLIENT_LIMIT)
