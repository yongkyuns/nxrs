"""Compile and exercise the native BLE scan bridge against small NuttX stubs."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "hal/ble/ffi/nuttx/nxrs_ble_scan.c"


class NativeBleTests(unittest.TestCase):
    def test_bridge_contract(self):
        registration = (ROOT / "external/nuttx/net/netdev/netdev_register.c").read_text()
        self.assertRegex(registration, re.compile(r"case NET_LL_BLUETOOTH:.*?devfmt\s*=\s*NETDEV_BNEP_FORMAT;", re.S))
        interface_format = re.search(r'#define NETDEV_BNEP_FORMAT\s+"([^"]+)"', registration)[1]
        expected_name = interface_format.replace("%d", "0")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "stub.h").write_text(r'''#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define IFNAMSIZ 16
#define AF_BLUETOOTH 31
#define SOCK_RAW 3
#define BTPROTO_L2CAP 0
#define SIOCGBTINFO 10
#define SIOCBTSCANSTART 11
#define SIOCBTSCANGET 12
#define SIOCBTSCANSTOP 13
struct bt_scanresponse_s { struct { uint8_t val[6], type; } sr_addr; int8_t sr_rssi; uint8_t sr_len, sr_data[64]; };
struct btreq_s { char btr_name[IFNAMSIZ]; bool btr_dupenable; uint8_t btr_nrsp; struct bt_scanresponse_s *btr_rsp; };
int socket(int, int, int);
int ioctl(int, unsigned long, unsigned long);
int close(int);
''')
            for header in ("nuttx/config.h", "sys/ioctl.h", "sys/socket.h", "unistd.h",
                           "netpacket/bluetooth.h", "nuttx/wireless/bluetooth/bt_ioctl.h"):
                path = root / header
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#include "stub.h"\n')
            harness = root / "test.c"
            harness.write_text(f'''#include <errno.h>
#include <string.h>
#define EXPECTED_NAME "{expected_name}"
#include "{SOURCE}"
static int command, calls, close_calls, socket_error, result, response_count, data_len;
int socket(int domain, int type, int protocol) {{ if (domain != AF_BLUETOOTH || type != SOCK_RAW || protocol != BTPROTO_L2CAP) return -1; if (socket_error) {{ errno = socket_error; return -1; }} return 7; }}
int ioctl(int fd, unsigned long cmd, unsigned long arg) {{
  struct btreq_s *r = (void *)(uintptr_t)arg; calls++; command = cmd;
  if (fd != 7 || strcmp(r->btr_name, EXPECTED_NAME)) return -1;
  if (cmd == SIOCBTSCANSTART && !r->btr_dupenable) return -1;
  if (result) {{ errno = result; return -1; }}
  if (cmd == SIOCBTSCANGET) {{ r->btr_nrsp = response_count; if (response_count && response_count <= 8) {{ struct bt_scanresponse_s *p = r->btr_rsp; p[0].sr_addr.val[0] = 42; p[0].sr_addr.type = 2; p[0].sr_rssi = -9; p[0].sr_len = data_len; p[0].sr_data[0] = 99; }} }}
  return 0;
}}
int close(int fd) {{ if (fd != 7) return -1; close_calls++; return 0; }}
#define CHECK(x) do {{ if (!(x)) return __LINE__; }} while (0)
int main(void) {{
  struct nxrs_ble_scan_record records[2] = {{0}}; uint8_t count = 77; int before;
  CHECK(nxrs_ble_scan_open() == 7); socket_error = EACCES; CHECK(nxrs_ble_scan_open() == -EACCES); socket_error = 0;
  CHECK(nxrs_ble_scan_check(7) == 0 && command == SIOCGBTINFO);
  CHECK(nxrs_ble_scan_start(7) == 0 && command == SIOCBTSCANSTART);
  response_count = 1; data_len = 1; CHECK(nxrs_ble_scan_get(7, records, 2, &count) == 0 && command == SIOCBTSCANGET);
  CHECK(count == 1 && records[0].address[0] == 42 && records[0].address_type == 2 && records[0].rssi == -9 && records[0].length == 1 && records[0].data[0] == 99);
  before = calls; CHECK(nxrs_ble_scan_get(7, records, 0, &count) == EINVAL && nxrs_ble_scan_get(7, records, 9, &count) == EINVAL);
  CHECK(nxrs_ble_scan_get(7, 0, 1, &count) == EINVAL && nxrs_ble_scan_get(7, records, 1, 0) == EINVAL && calls == before && count == 1);
  response_count = 2; CHECK(nxrs_ble_scan_get(7, records, 1, &count) == EOVERFLOW);
  response_count = 1; data_len = 65; CHECK(nxrs_ble_scan_get(7, records, 1, &count) == EOVERFLOW);
  result = EACCES; CHECK(nxrs_ble_scan_stop(7) == EACCES && command == SIOCBTSCANSTOP); result = 0;
  nxrs_ble_scan_close(-1); CHECK(close_calls == 0); nxrs_ble_scan_close(7); CHECK(close_calls == 1);
  return 0;
}}
''')
            binary = root / "test"
            subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-pedantic", "-I", str(root), str(harness), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
