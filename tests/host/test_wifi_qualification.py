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
