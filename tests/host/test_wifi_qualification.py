"""Protect runtime credential handling and the exact echo packet contract."""
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock

PATH = Path(__file__).resolve().parents[1] / 'nuttx-std/wifi/run.py'
SPEC = importlib.util.spec_from_file_location('wifi_qualification', PATH)
WIFI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WIFI)


class WifiQualificationTests(unittest.TestCase):
    def test_station_configures_mode_before_authentication_and_stops_on_errors(self):
        native = (WIFI.ROOT / 'hal/wifi/src/platform/nuttx/ffi/wifi_station.c').read_text()
        helpers = native[native.index('static int named('):
                         native.index('int wifi_station_available(')]
        connect = native[native.index('int wifi_station_infra('):
                         native.index('int wifi_station_address(')]
        # Execute the real helper with a mock wireless ioctl ABI. The separate
        # target ABI witnesses validate the actual NuttX header layouts.
        source = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#define IFNAMSIZ 16
enum { SIOCSIWMODE, SIOCSIWAUTH, SIOCSIWENCODEEXT, SIOCSIWESSID };
enum { IW_MODE_INFRA = 2, IW_AUTH_WPA_VERSION, IW_AUTH_CIPHER_PAIRWISE,
       IW_AUTH_WPA_VERSION_DISABLED, IW_AUTH_WPA_VERSION_WPA2,
       IW_AUTH_CIPHER_NONE, IW_AUTH_CIPHER_CCMP, IW_ENCODE_ALG_NONE,
       IW_ENCODE_ALG_CCMP, IW_ENCODE_DISABLED, IW_ESSID_ON };
struct iwreq {
  char ifr_name[IFNAMSIZ];
  union {
    int mode;
    struct { int flags, value; } param;
    struct { void *pointer; unsigned length, flags; } encoding, essid;
  } u;
};
struct iw_encode_ext { int alg; unsigned key_len; uint8_t key[]; };
static unsigned calls, fail_at, open_network;
static int fake_ioctl(int fd, int command, unsigned long argument) {
  static const int commands[] = { SIOCSIWMODE, SIOCSIWAUTH, SIOCSIWAUTH,
                                  SIOCSIWENCODEEXT, SIOCSIWESSID };
  const struct iwreq *request = (void *)(uintptr_t)argument;
  unsigned step = calls++;
  assert(fd == 37 && step < 5 && command == commands[step]);
  assert(strcmp(request->ifr_name, "wlan0") == 0);
  switch (step) {
    case 0: assert(request->u.mode == IW_MODE_INFRA); break;
    case 1:
      assert(request->u.param.flags == IW_AUTH_WPA_VERSION);
      assert(request->u.param.value == (open_network ?
             IW_AUTH_WPA_VERSION_DISABLED : IW_AUTH_WPA_VERSION_WPA2));
      break;
    case 2:
      assert(request->u.param.flags == IW_AUTH_CIPHER_PAIRWISE);
      assert(request->u.param.value == (open_network ?
             IW_AUTH_CIPHER_NONE : IW_AUTH_CIPHER_CCMP));
      break;
    case 3: {
      const struct iw_encode_ext *key = request->u.encoding.pointer;
      unsigned length = open_network ? 0 : 8;
      assert(key->alg == (open_network ? IW_ENCODE_ALG_NONE : IW_ENCODE_ALG_CCMP));
      assert(key->key_len == length);
      assert(memcmp(key->key, "xxxxxxxx", length) == 0);
      assert(request->u.encoding.length == sizeof(*key) + length);
      assert(request->u.encoding.flags == (open_network ? IW_ENCODE_DISABLED : 0));
      break;
    }
    case 4:
      assert(request->u.essid.flags == IW_ESSID_ON);
      assert(request->u.essid.length == 3);
      assert(memcmp(request->u.essid.pointer, "lab", 3) == 0);
      break;
  }
  if (step == fail_at) { errno = EIO; return -1; }
  return 0;
}
#define ioctl fake_ioctl
#define explicit_bzero(pointer, count) memset(pointer, 0, count)
''' + helpers + connect + r'''
/* Witness calls individual ABI helpers; production sequencing is tested in Rust. */
static int connect_fields(int fd, const char *name, const uint8_t *ssid, size_t length, const uint8_t *key, size_t key_length) {
  if (!length || length>32 || key_length>63 || (key_length && key_length<8)) return EINVAL;
  int result = wifi_station_infra(fd,name); if (result) return result;
  result = wifi_station_wpa(fd,name,key_length!=0); if (result) return result;
  result = wifi_station_cipher(fd,name,key_length!=0); if (result) return result;
  result = wifi_station_key(fd,name,key,key_length); if (result) return result;
  return wifi_station_ssid(fd,name,ssid,length);
}

int main(void) {
  const uint8_t ssid[] = "lab", password[] = "xxxxxxxx";
  for (open_network = 0; open_network < 2; open_network++) {
    for (fail_at = 0; fail_at <= 5; fail_at++) {
      calls = 0;
      int result = connect_fields(37, "wlan0", ssid, 3,
                                            password, open_network ? 0 : 8);
      assert(result == (fail_at == 5 ? 0 : EIO));
      assert(calls == (fail_at == 5 ? 5 : fail_at + 1));
    }
  }
  calls = 0;
  assert(connect_fields(37, "wlan0", ssid, 0, password, 8) == EINVAL);
  assert(connect_fields(37, "wlan0", ssid, 3, password, 7) == EINVAL);
  assert(calls == 0);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'station-test.c').write_text(source)
            result = subprocess.run(['cc', '-Wall', '-Wextra', '-Werror',
                                     str(path / 'station-test.c'), '-o', str(path / 'station-test')],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'station-test')], check=True, capture_output=True)

    def test_software_only_tty_is_initialized_and_never_echoes_the_input_frame(self):
        source = r'''
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
static unsigned sets, ready, offset;
static const uint8_t frame[] = {3, 8, 'l', 'a', 'b', 'x', 'x', 'x',
                                'x', 'x', 'x', 'x', 'x'};
static int fake_get(int fd, struct termios *value) {
  (void)fd;
  /* Match the NuttX upper half: hardware fields are untouched. */
  assert(value->c_cflag == 0);
  value->c_iflag = ICRNL;
  value->c_oflag = 0;
  value->c_lflag = ECHO | ICANON;
  return 0;
}
static int fake_set(int fd, int action, const struct termios *value) {
  (void)fd; (void)action;
  assert(value->c_cflag == 0);
  if (sets++ == 0) assert((value->c_lflag & (ECHO | ICANON)) == 0);
  else assert((value->c_lflag & (ECHO | ICANON)) == (ECHO | ICANON));
  return 0;
}
static int fake_puts(const char *text) {
  assert(sets == 1);
  assert(strcmp(text, "NXRS_WIFI_CREDENTIALS_READY") == 0);
  ready = 1;
  return 0;
}
static int fake_poll(struct pollfd *fds, nfds_t count, int timeout) {
  (void)timeout; assert(ready && sets == 1 && count == 1);
  fds->revents = POLLIN;
  return 1;
}
static ssize_t fake_read(int fd, void *destination, size_t count) {
  (void)fd; assert(ready && sets == 1);
  if (count > 2) count = 2; /* Exercise partial USB reads. */
  assert(offset + count <= sizeof(frame));
  memcpy(destination, frame + offset, count);
  offset += count;
  return (ssize_t)count;
}
#define tcgetattr fake_get
#define tcsetattr fake_set
#define puts fake_puts
#define poll fake_poll
#define read fake_read
#define explicit_bzero(pointer, count) memset(pointer, 0, count)
#include "credentials.c"
int main(void) {
  uint8_t ssid[32], password[63];
  uint32_t ssid_length, password_length;
  assert(nxrs_wifi_test_credentials(ssid, password, &ssid_length,
                                    &password_length) == 0);
  assert(sets == 2 && offset == sizeof(frame));
  assert(ssid_length == 3 && password_length == 8);
  assert(memcmp(ssid, frame + 2, 3) == 0);
  assert(memcmp(password, frame + 5, 8) == 0);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'tty-test.c').write_text(source)
            result = subprocess.run(['cc', '-Wall', '-Wextra', '-Werror',
                         '-I', str(PATH.parent / 'ffi'), str(path / 'tty-test.c'),
                         '-o', str(path / 'tty-test')], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'tty-test')], check=True, capture_output=True)

    def test_private_file_accepts_only_one_valid_passphrase(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'password'
            for value in (b'test-only-passphrase', b'test-only-passphrase\n'):
                path.write_bytes(value)
                path.chmod(0o600)
                self.assertEqual(WIFI.password_bytes(path), b'test-only-passphrase')
            for value in (b'short', b'x' * 64, b'valid-but\nmultiline', b'control-\x01-byte'):
                path.write_bytes(value)
                with self.assertRaises(ValueError):
                    WIFI.password_bytes(path)

    def test_public_and_symlink_password_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'password'
            path.write_bytes(b'test-only-passphrase')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                WIFI.password_bytes(path)
            path.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                WIFI.password_bytes(link)

    def test_repository_paths_are_rejected_before_open(self):
        with self.assertRaises(ValueError):
            WIFI.password_bytes(WIFI.ROOT / 'not-a-real-password')

    def test_packet_lengths_sequences_and_payloads_are_exact(self):
        packets = [WIFI.expected_packet(sequence) for sequence in range(20)]
        self.assertEqual(len(set(packets)), 20)
        for sequence, packet in enumerate(packets):
            self.assertEqual(len(packet), (16, 64, 256, 512)[sequence % 4])
            self.assertEqual(int.from_bytes(packet[:4], 'big'), sequence)
            self.assertEqual(packet[4:], bytes((offset + sequence) % 256
                                             for offset in range(len(packet) - 4)))

    def test_serial_markers_share_a_read_without_losing_the_next_stage(self):
        console = Mock()
        console.read.return_value = b'first\nsecond\nnsh> '
        pending = bytearray()
        self.assertEqual(WIFI.read_until(console, pending, b'first', b'', 1), b'first')
        self.assertEqual(WIFI.read_until(console, pending, b'second', b'', 1), b'\nsecond')
        self.assertEqual(WIFI.read_until(console, pending, b'nsh> ', b'', 1), b'\nnsh> ')
        console.read.assert_called_once()

    def test_serial_failures_never_expose_raw_data_or_accept_a_pass(self):
        secret = b'test-only-passphrase'
        for data in (secret + b'NXRS_WIFI_STATION_PASS',
                     b'NXRS_WIFI_STATION_FAIL NXRS_WIFI_STATION_PASS'):
            with self.assertRaises(RuntimeError) as failure:
                WIFI.read_until(Mock(), bytearray(data), b'NXRS_WIFI_STATION_PASS', secret, 1)
            self.assertNotIn(secret.decode(), str(failure.exception))

    def test_traffic_diagnostic_exposes_only_stage_sequence_and_kind(self):
        data = (b'unrelated-private-console-data\n'
                b'NXRS_WIFI_TRAFFIC_FAIL phase=receive sequence=0 kind=WouldBlock\n'
                b'NXRS_WIFI_STATION_FAIL')
        with self.assertRaisesRegex(RuntimeError, 'traffic receive sequence 0: WouldBlock') as failure:
            WIFI.read_until(Mock(), bytearray(data), b'NXRS_WIFI_STATION_PASS', b'', 1)
        self.assertNotIn('unrelated-private-console-data', str(failure.exception))

    def test_station_error_diagnostic_is_a_whitelisted_enum_not_raw_text(self):
        for kind, expected in ((b'AuthenticationFailed', '; AuthenticationFailed'),
                               (b'private-console-data', 'no pass')):
            data = b'NXRS_WIFI_STATION_FAIL error=' + kind + b'\n'
            with self.assertRaisesRegex(RuntimeError, expected) as failure:
                WIFI.read_until(Mock(), bytearray(data), b'NXRS_WIFI_STATION_PASS', b'', 1)
            self.assertNotIn('private-console-data', str(failure.exception))

    def test_arp_diagnostic_does_not_expose_addresses_or_other_console_data(self):
        data = (b'arp_wait failed: -110, ipaddr: private-address\n'
                b'NXRS_WIFI_STATION_FAIL error=Io\n')
        with self.assertRaisesRegex(RuntimeError, 'ARP wait error -110') as failure:
            WIFI.read_until(Mock(), bytearray(data), b'NXRS_WIFI_STATION_PASS', b'', 1)
        self.assertNotIn('private-address', str(failure.exception))

    def test_credential_echo_takes_priority_over_traffic_diagnostics(self):
        secret = b'test-only-passphrase'
        data = (b'NXRS_WIFI_TRAFFIC_FAIL phase=send sequence=0 kind=Other\n'
                + secret + b'NXRS_WIFI_STATION_FAIL')
        with self.assertRaisesRegex(RuntimeError, 'credential echo detected') as failure:
            WIFI.read_until(Mock(), bytearray(data), b'NXRS_WIFI_STATION_PASS', secret, 1)
        self.assertNotIn(secret.decode(), str(failure.exception))


if __name__ == '__main__':
    unittest.main()
