"""Exact GATT/UDP contracts and sanitized serial parsing, without radios."""
import importlib.util
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'nuttx-std/radio/run.py'
SPEC = importlib.util.spec_from_file_location('radio_runner', PATH)
RADIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADIO)


class RadioQualificationTests(unittest.TestCase):
    def test_ble_only_cli_never_reads_wifi_credentials(self):
        for options, expected in [(['--ble-only'], 0),
                                  ([], 2),
                                  (['--ble-only', '--ssid', 'unused'], 2),
                                  (['--ble-only', '--study', 'coexist'], 2),
                                  (['--ble-only', '--transport', 'tcp'], 2)]:
            with self.subTest(options=options), \
                    patch('sys.argv', ['run.py', '--port', 'fixture-port', *options]), \
                    patch.object(RADIO, 'qualify_ble') as ble, \
                    patch.object(RADIO, 'qualify') as wifi, \
                    patch.object(RADIO.WIFI, 'password_bytes') as credentials, \
                    patch('sys.stderr'):
                if expected:
                    with self.assertRaises(SystemExit) as error:
                        RADIO.main()
                    self.assertEqual(error.exception.code, expected)
                    ble.assert_not_called()
                else:
                    RADIO.main()
                    ble.assert_called_once_with('fixture-port')
                wifi.assert_not_called()
                credentials.assert_not_called()

    def test_ble_only_uses_the_same_gatt_workload_without_network_or_credentials(self):
        console = MagicMock()
        serial = Mock()
        serial.Serial.return_value = console
        console.__enter__.return_value = console
        for status, passed in ((b'\r\n0\r\nnsh> ', True),
                               (b'\r\n1\r\nnsh> ', False),
                               (b'\r\n0\r\n1\r\nnsh> ', False),
                               (b'\r\n0\r\n0\r\nnsh> ', False),
                               (b'echo $?\r\nnsh> ', False)):
            with self.subTest(status=status), patch.dict('sys.modules', serial=serial), \
                    patch.object(RADIO, 'gatt', new_callable=AsyncMock) as gatt, \
                    patch.object(RADIO, 'until', side_effect=[
                        b'nsh> ', b'NXRS_RADIO_GATT_READY', b'NXRS_RADIO_GATT_DONE',
                        b'NXRS_RADIO_PASS', b'nsh> ', status]), \
                    patch.object(RADIO.WIFI, 'password_bytes') as credentials, \
                    patch.object(RADIO.socket, 'socket', wraps=socket.socket) as sockets, \
                    patch('builtins.print'):
                console.reset_mock()
                if passed:
                    RADIO.qualify_ble('fixture-port')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'exit status was not zero'):
                        RADIO.qualify_ble('fixture-port')
                gatt.assert_awaited_once_with()
                credentials.assert_not_called()
                # asyncio owns an AF_UNIX self-pipe; it is not radio traffic.
                self.assertTrue(all(call.args[0] not in (socket.AF_INET, socket.AF_INET6)
                                    for call in sockets.call_args_list))
                self.assertEqual([call.args[0] for call in console.write.call_args_list],
                                 [b'\n\n\n', b'radio_test ble-only\n', b'done\n', b'echo $?\n'])

    def test_ble_only_bypasses_wifi_but_keeps_native_cleanup_and_exact_observer(self):
        source = (PATH.parent / 'src/device.rs').read_text()
        run = source[source.index('pub(super) fn run()'):source.index('fn wifi_only(')]
        self.assertLess(run.index('return ble_only();'),
                        run.index('wifi_scan_owner::station::with_connection_observed'))
        ble = source[source.index('fn ble_only()'):source.index('fn wifi_only(')]
        self.assertIn('observe(None, super::RunMode::Qualification)', ble)
        self.assertIn('start_gatt(None)?;', ble)
        self.assertLess(ble.index('stop_gatt(None)'), ble.index('result?;'))
        self.assertNotIn('TrafficRun::spawn', ble)

    def test_retired_socket_controls_are_rejected_before_device_access(self):
        for option in ('--socket-implementation', '--tcp-implementation'):
            with self.subTest(option=option), \
                    patch('sys.argv', ['run.py', '--port', 'fixture-port', option, 'c']), \
                    patch.object(RADIO, 'open_console') as console, \
                    patch.object(RADIO.WIFI, 'password_bytes') as credentials, \
                    patch('sys.stderr'):
                with self.assertRaises(SystemExit) as error:
                    RADIO.main()
                self.assertEqual(error.exception.code, 2)
                console.assert_not_called()
                credentials.assert_not_called()

    def test_join_requires_a_stopped_worker_before_success(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'worker did not stop'):
            RADIO.join_worker(worker)
        worker.join.assert_called_once_with(timeout=2)
        worker.is_alive.return_value = False
        RADIO.join_worker(worker)

    def test_native_uuids_match_the_host_filter_and_characteristic(self):
        source = (PATH.parent / 'ffi/gatt.c').read_text()
        for name, expected in (('legacy_service', '4e585253-0001-4000-8000-000000000001'),
                               ('service', '4e585253-0002-4000-8000-000000000001'),
                               ('characteristic', '4e585253-0001-4000-8000-000000000002')):
            block = source.split(f'g_{name}_uuid =', 1)[1].split('};', 1)[0]
            raw = bytes(int(value, 16) for value in re.findall(r'0x([0-9a-fA-F]{2})', block))
            self.assertEqual(raw, uuid.UUID(expected).bytes[::-1])
        self.assertEqual(RADIO.SERVICE, '4e585253-0002-4000-8000-000000000001')
        self.assertEqual(RADIO.LEGACY_SERVICE, '4e585253-0001-4000-8000-000000000001')

    def test_native_callbacks_reject_bad_writes_and_count_complete_readbacks(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        callbacks = native[native.index('static int nxrs_radio_gatt_value_read('):
                           native.index('static struct bt_gatt_attr_s g_attributes')]
        source = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
struct bt_conn_s { int identity; }; struct bt_gatt_attr_s;
#define NXRS_RADIO_VALUE_LEN 20
#define NXRS_RADIO_ROUNDS 100
enum { NXRS_RADIO_GATT_STOPPED, NXRS_RADIO_GATT_STARTING, NXRS_RADIO_GATT_RUNNING };
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_state = NXRS_RADIO_GATT_RUNNING, g_has_value, g_value_read;
static uint8_t g_value[20];
static uint32_t g_writes, g_reads, g_invalid;
static struct bt_conn_s *g_selected_conn;
static int g_peer_lost;
''' + callbacks + r'''
int main(void) {
  struct bt_conn_s selected = {1};
  uint8_t packet[20] = {0}, readback[20];
  assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 19, 0) == -EINVAL);
  assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 20, 1) == -EINVAL);
  packet[19] = 0xff;
  assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 20, 0) == -EINVAL);
  assert(g_writes == 0 && g_invalid == 3);
  for (unsigned sequence = 0; sequence < 100; sequence++) {
    packet[3] = sequence;
    for (unsigned i = 0; i < 16; i++) packet[4+i] = sequence+i;
    assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 20, 0) == 20);
    assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 20, 0) == -EINVAL);
    assert(nxrs_radio_gatt_value_read(&selected, NULL, readback, 10, 0) == 10);
    assert(g_reads == sequence);  /* Partial reads cannot satisfy the test. */
    assert(nxrs_radio_gatt_value_read(&selected, NULL, readback, 20, 0) == 20);
    assert(memcmp(packet, readback, 20) == 0);
    assert(g_reads == sequence+1);
    assert(nxrs_radio_gatt_value_read(&selected, NULL, readback, 20, 0) == 20);
    assert(g_reads == sequence+1); /* Duplicate reads cannot inflate evidence. */
  }
  assert(g_writes == 100 && g_reads == 100);
  g_state = NXRS_RADIO_GATT_STOPPED;
  assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 20, 0) == -EACCES);
  assert(nxrs_radio_gatt_value_read(&selected, NULL, readback, 20, 0) < 0);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'callbacks.c').write_text(source)
            result = subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-pthread',
                                     str(path / 'callbacks.c'), '-o', str(path / 'callbacks')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'callbacks')], check=True, capture_output=True)

    def test_native_extended_gatt_reads_cover_boundaries_fragments_and_peer_state(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        callback = native[
            native.index('static int nxrs_radio_gatt_extended_read('):
            native.index('static int nxrs_radio_gatt_value_read(')]
        source = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
struct bt_conn_s { int identity; };
struct bt_gatt_attr_s { void *user_data; };
enum { NXRS_RADIO_GATT_STOPPED, NXRS_RADIO_GATT_STARTING,
       NXRS_RADIO_GATT_RUNNING };
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_state = NXRS_RADIO_GATT_RUNNING;
static int g_has_value;
static struct bt_conn_s *g_selected_conn;
static int g_peer_lost;
''' + callback + r'''
int main(void) {
  struct bt_conn_s selected = {1}, foreign = {2};
  uint16_t lengths[] = {44, 64, 65};
  struct bt_gatt_attr_s attrs[] = {
    {&lengths[0]}, {&lengths[1]}, {&lengths[2]}
  };
  uint8_t data[80];
  g_selected_conn = &selected;

  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[0], data, 80, 0) == -EAGAIN);
  g_has_value = 1;
  assert(nxrs_radio_gatt_extended_read(NULL, &attrs[0], data, 44, 0) == -EACCES);
  assert(nxrs_radio_gatt_extended_read(&foreign, &attrs[0], data, 44, 0) == -EACCES);
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[0], data, 80, 0) == 44);
  for (unsigned i = 0; i < 44; i++) assert(data[i] == (uint8_t)(44 + i));
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[0], data, 8, 44) == 0);
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[0], data, 1, 45) == -EINVAL);

  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[1], data, 17, 0) == 17);
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[1], data, 29, 17) == 29);
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[1], data, 30, 46) == 18);
  for (unsigned i = 0; i < 18; i++) assert(data[i] == (uint8_t)(64 + 46 + i));
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[1], data, 0, 64) == 0);

  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[2], data, 255, 0) == 65);
  for (unsigned i = 0; i < 65; i++) assert(data[i] == (uint8_t)(65 + i));
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[2], data, 2, 64) == 1);
  assert(data[0] == (uint8_t)129);
  g_peer_lost = 1;
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[2], data, 1, 0) == -EACCES);
  g_peer_lost = 0;
  g_state = NXRS_RADIO_GATT_STOPPED;
  assert(nxrs_radio_gatt_extended_read(&selected, &attrs[2], data, 1, 0) == -EACCES);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'extended.c').write_text(source)
            result = subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-pthread',
                                     str(path / 'extended.c'), '-o', str(path / 'extended')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'extended')], check=True, capture_output=True)

    def test_all_gatt_modes_keep_one_stable_complete_database(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        registration = re.search(r'\bbt_gatt_register\([^;]+;', native).group()
        begin = native.index('static struct bt_gatt_attr_s g_attributes[]')
        table = native[begin:native.index('\n};', begin) + 3]
        declarations = native[native.index('static const struct bt_uuid_s g_legacy_service_uuid ='):
                              native.index('static int nxrs_radio_gatt_extended_read(')]
        for path in (PATH.parent / 'ffi/gatt.h', PATH.parent / 'src/device.rs'):
            self.assertNotIn('gatt_start_values', path.read_text())
        self.assertNotIn('attribute_count', native)
        source = '''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define BT_UUID_128 1
#define NXRS_RADIO_EXTENDED_VALUES 3
#define BT_GATT_CHRC_READ 1
#define BT_GATT_CHRC_WRITE 2
#define BT_GATT_PERM_READ 1
#define BT_GATT_PERM_WRITE 2
struct bt_uuid_s { int type; union { uint8_t u128[16]; } u; };
struct bt_gatt_chrc_s { const struct bt_uuid_s *uuid; unsigned value_handle, properties; };
static int nxrs_radio_gatt_value_read(void) { return 0; }
static int nxrs_radio_gatt_value_write(void) { return 0; }
static int nxrs_radio_gatt_extended_read(void) { return 0; }
struct bt_gatt_attr_s {
  unsigned handle, perm;
  const void *uuid;
  int (*read)(void), (*write)(void);
  const void *user_data;
};
#define BT_GATT_PRIMARY_SERVICE(h,u) { .handle=(h), .uuid=(u) }
#define BT_GATT_CHARACTERISTIC(h,u) { .handle=(h), .user_data=(u) }
''' + declarations + table + '''
static unsigned registrations;
static void bt_gatt_register(const struct bt_gatt_attr_s *attrs, size_t count) {
  assert(attrs == g_attributes && count == 12);
  for (size_t i = 0; i < count; i++) assert(attrs[i].handle == i + 1);
  assert(attrs[0].uuid == &g_legacy_service_uuid);
  assert(attrs[3].uuid == &g_service_uuid);
  assert(attrs[0].perm == 0 && attrs[3].perm == 0);
  assert(attrs[1].user_data == &g_legacy_characteristic);
  assert(attrs[4].user_data == &g_characteristic);
  assert(g_legacy_characteristic.value_handle == 3);
  assert(g_characteristic.value_handle == 6);
  assert(g_legacy_characteristic.properties == (BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE));
  assert(g_characteristic.properties == (BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE));
  assert(attrs[1].handle == 2 && attrs[2].handle == 3);
  assert(attrs[4].handle == 5 && attrs[5].handle == 6);
  assert(attrs[2].uuid == &g_characteristic_uuid && attrs[5].uuid == &g_characteristic_uuid);
  assert(attrs[2].read == nxrs_radio_gatt_value_read);
  assert(attrs[5].read == nxrs_radio_gatt_value_read);
  assert(attrs[2].write == nxrs_radio_gatt_value_write);
  assert(attrs[5].write == nxrs_radio_gatt_value_write);
  assert(attrs[2].perm == (BT_GATT_PERM_READ | BT_GATT_PERM_WRITE));
  assert(attrs[5].perm == (BT_GATT_PERM_READ | BT_GATT_PERM_WRITE));
  for (unsigned i = 0; i < 3; i++) {
    unsigned declaration = 6 + 2*i, value = declaration + 1;
    uint8_t expected_uuid[16] = {
      (uint8_t)(3 + i), 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
      0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e
    };
    assert(attrs[declaration].handle == 7 + 2*i);
    assert(attrs[declaration].user_data == &g_extended_characteristics[i]);
    assert(g_extended_characteristics[i].uuid == &g_extended_uuids[i]);
    assert(g_extended_characteristics[i].value_handle == value + 1);
    assert(g_extended_characteristics[i].properties == BT_GATT_CHRC_READ);
    assert(memcmp(g_extended_uuids[i].u.u128, expected_uuid,
                  sizeof(expected_uuid)) == 0);
    assert(attrs[value].handle == 8 + 2*i);
    assert(attrs[value].uuid == &g_extended_uuids[i]);
    assert(attrs[value].read == nxrs_radio_gatt_extended_read);
    assert(attrs[value].write == NULL);
    assert(attrs[value].user_data == &g_extended_lengths[i]);
    assert(attrs[value].perm == BT_GATT_PERM_READ);
  }
  registrations++;
}
int main(void) {
''' + registration + '''
  assert(registrations == 1);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            witness = directory / 'stable.c'
            witness.write_text(source)
            binary = directory / 'stable'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                            str(witness), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_native_advertisement_packets_are_bounded_and_advertise_v1_v2(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        begin = native.index('  advertisement[0].len =')
        end = native.index('  memcpy(request.btr_name,', begin)
        initialization = native[begin:end]
        declarations = native[native.index('static const struct bt_uuid_s g_legacy_service_uuid ='):
                              native.index('static const struct bt_uuid_s g_characteristic_uuid =')]
        arrays = re.findall(r'  struct bt_eir_s (?:advertisement|scan_response)\[[0-9]+\] = \{0\};', native)
        self.assertEqual(len(arrays), 2)
        name = re.search(r'^#define NXRS_RADIO_ADV_NAME\s+"[^"\n]+"', native, re.MULTILINE).group()
        source = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#define BT_UUID_128 1
#define BT_EIR_FLAGS 1
#define BT_EIR_UUID128_SOME 2
#define BT_EIR_NAME_COMPLETE 3
#define BT_LE_AD_GENERAL 2
#define BT_LE_AD_NO_BREDR 4
struct bt_uuid_s { int type; union { uint8_t u128[16]; } u; };
struct bt_eir_s { uint8_t len, type, data[29]; };
''' + name + '\n' + declarations + r'''
int main(void) {
''' + '\n'.join(arrays) + '\n' + initialization + r'''
  uint8_t v2[16] = { 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
                     0x00, 0x40, 0x02, 0x00, 0x53, 0x52, 0x58, 0x4e };
  uint8_t v1[16] = { 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
                     0x00, 0x40, 0x01, 0x00, 0x53, 0x52, 0x58, 0x4e };
  unsigned adv_size = 0, scan_size = 0;
  for (unsigned i = 0; i < 3; i++) {
    if (advertisement[i].len) adv_size += advertisement[i].len + 1;
    else assert(advertisement[i].type == 0);
    if (scan_response[i].len) scan_size += scan_response[i].len + 1;
    else assert(scan_response[i].type == 0);
  }
  assert(adv_size <= 31 && scan_size <= 31);
  assert(adv_size == 21 && scan_size == 29);
  assert(advertisement[0].type == BT_EIR_FLAGS && advertisement[0].len == 2);
  assert(advertisement[0].data[0] == (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR));
  assert(advertisement[1].type == BT_EIR_UUID128_SOME && advertisement[1].len == 17);
  assert(memcmp(advertisement[1].data, v2, sizeof(v2)) == 0);
  assert(scan_response[0].type == BT_EIR_UUID128_SOME && scan_response[0].len == 17);
  assert(memcmp(scan_response[0].data, v1, sizeof(v1)) == 0);
  assert(scan_response[1].type == BT_EIR_NAME_COMPLETE && scan_response[1].len == sizeof("nxrs-gatt"));
  assert(memcmp(scan_response[1].data, "nxrs-gatt", sizeof("nxrs-gatt") - 1) == 0);
  assert(advertisement[2].len == 0 && scan_response[2].len == 0);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            witness = directory / 'advertising.c'
            witness.write_text(source)
            binary = directory / 'advertising'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                            str(witness), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_native_connection_callbacks_track_only_the_selected_peer(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        connection_callbacks = native[
            native.index('static void nxrs_radio_gatt_connected('):
            native.index('/* The UUID byte arrays')]
        snapshot = native[
            native.index('int nxrs_radio_gatt_snapshot('):
            native.index('int nxrs_radio_gatt_connections(')]
        connections = native[
            native.index('int nxrs_radio_gatt_connections('):
            native.index('int nxrs_radio_gatt_wait_finish(')]
        source = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
struct bt_conn_s { int identity; uint8_t disconn_reason; };
typedef unsigned long irqstate_t;
enum { BT_ADDR_LE_ANY, BT_CONN_CONNECTED };
static struct bt_conn_s native_conn = {.identity=3};
static int critical_depth, lookup_in_critical, release_count, native_present = 1;
static irqstate_t enter_critical_section(void) { critical_depth++; return 7; }
static void leave_critical_section(irqstate_t flags) { assert(flags == 7); critical_depth--; }
static struct bt_conn_s *bt_conn_lookup_state(int address, int state) {
  assert(critical_depth == 1); lookup_in_critical++;
  assert(address == BT_ADDR_LE_ANY && state == BT_CONN_CONNECTED);
  return native_present ? &native_conn : NULL;
}
static void bt_conn_release(struct bt_conn_s *conn) {
  assert(conn == &native_conn && critical_depth == 0); release_count++;
}
#include <string.h>
struct bt_gatt_attr_s;
struct bt_gatt_conn_cb_s {
  struct bt_gatt_conn_cb_s *flink;
  void (*connected)(struct bt_conn_s *, void *);
  void (*disconnected)(struct bt_conn_s *, void *);
  void *arg;
};
enum nxrs_radio_gatt_state_e { NXRS_RADIO_GATT_STOPPED,
       NXRS_RADIO_GATT_STARTING, NXRS_RADIO_GATT_RUNNING,
       NXRS_RADIO_GATT_STOPPING };
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static enum nxrs_radio_gatt_state_e g_state = NXRS_RADIO_GATT_RUNNING;
static struct bt_conn_s *g_selected_conn;
static int g_peer_lost;
static uint32_t g_writes, g_reads, g_invalid;
static uint32_t g_connections, g_disconnections;
static int32_t g_disconnect_reason = -1;
static uint8_t g_value[20];
static int g_has_value, g_value_read;
#define NXRS_RADIO_VALUE_LEN 20
#define NXRS_RADIO_ROUNDS 100
''' + connection_callbacks + native[native.index('static int nxrs_radio_gatt_value_read('):
                                    native.index('static struct bt_gatt_attr_s g_attributes')] + snapshot + r'''
''' + connections + r'''
int main(void) {
  struct bt_conn_s selected = {.identity=1, .disconn_reason=19};
  struct bt_conn_s foreign = {.identity=2, .disconn_reason=8};
  uint32_t writes, reads, invalid, lost, connected, disconnected, native_connected;
  int32_t reason = -1;
  uint8_t packet[20] = {0}, value[20];
  for (unsigned i = 0; i < 16; i++) packet[4+i] = i;
  assert(g_conn_cb.connected == nxrs_radio_gatt_connected);
  assert(g_disconnect_reason == -1);
  nxrs_radio_gatt_connected(&foreign, NULL);
  assert(g_connections == 1 && g_selected_conn == NULL);
  nxrs_radio_gatt_disconnected(&foreign, NULL);
  assert(g_disconnections == 1);
  assert(g_disconnect_reason == 8);
  assert(!g_peer_lost && g_selected_conn == NULL);
  assert(nxrs_radio_gatt_value_write(&selected, NULL, packet, 20, 0) == 20);
  assert(nxrs_radio_gatt_value_read(&foreign, NULL, value, 20, 0) == -EACCES);
  assert(g_reads == 0);
  assert(nxrs_radio_gatt_value_write(&foreign, NULL, packet, 20, 0) == -EACCES);
  assert(g_invalid == 0 && g_writes == 1);
  assert(nxrs_radio_gatt_value_read(&selected, NULL, value, 20, 0) == 20);
  nxrs_radio_gatt_disconnected(&foreign, NULL);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, &lost) == 0);
  assert(lost == 0);
  nxrs_radio_gatt_disconnected(&selected, NULL);
  nxrs_radio_gatt_disconnected(&selected, NULL);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, &lost) == 0);
  assert(lost == 1);
  assert(nxrs_radio_gatt_snapshot(NULL, &reads, &invalid, &lost) == EINVAL);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, NULL) == EINVAL);
  connected = 91; disconnected = 92; native_connected = 93;
  assert(nxrs_radio_gatt_connections(NULL, &disconnected, &native_connected, &reason) == EINVAL);
  assert(nxrs_radio_gatt_connections(&connected, NULL, &native_connected, &reason) == EINVAL);
  assert(nxrs_radio_gatt_connections(&connected, &disconnected, NULL, &reason) == EINVAL);
  assert(nxrs_radio_gatt_connections(&connected, &disconnected, &native_connected, NULL) == EINVAL);
  assert(connected == 91 && disconnected == 92 && native_connected == 93);
  assert(nxrs_radio_gatt_connections(&connected, &disconnected, &native_connected, &reason) == 0);
  assert(reason == 19);
  assert(connected == 1 && disconnected == 4 && native_connected == 1);
  assert(lookup_in_critical == 1 && critical_depth == 0 && release_count == 1);
  native_present = 0;
  assert(nxrs_radio_gatt_connections(&connected, &disconnected, &native_connected, &reason) == 0);
  assert(native_connected == 0 && connected == 1 && disconnected == 4);
  assert(lookup_in_critical == 2 && critical_depth == 0 && release_count == 1);
  nxrs_radio_gatt_disconnected(NULL, NULL);
  assert(g_disconnect_reason == -1);
  /* A new run clears the sticky identity and loss state. */
  g_state = NXRS_RADIO_GATT_STARTING;
  g_selected_conn = NULL;
  g_peer_lost = 0;
  g_writes = 0;
  g_has_value = g_value_read = 0;
  assert(nxrs_radio_gatt_value_write(&foreign, NULL, packet, 20, 0) == 20);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, &lost) == 0);
  assert(lost == 0);
  assert(g_selected_conn == &foreign);
  return 0;
}
'''
        # Keep the extracted implementation coupled to start's per-run reset.
        start = native[native.index('int nxrs_radio_gatt_start(void)'):
                       native.index('int nxrs_radio_gatt_snapshot(')]
        self.assertIn('g_selected_conn = NULL;', start)
        self.assertIn('g_peer_lost = 0;', start)
        self.assertIn('g_connections = 0;', start)
        self.assertIn('g_disconnections = 0;', start)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'callbacks.c').write_text(source)
            result = subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-pthread',
                                     str(path / 'callbacks.c'), '-o', str(path / 'callbacks')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'callbacks')], check=True, capture_output=True)

    def test_connection_callback_registration_brackets_advertising_lifecycle(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        start = native[native.index('int nxrs_radio_gatt_start(void)'):
                       native.index('int nxrs_radio_gatt_snapshot(')]
        stop = native[native.index('int nxrs_radio_gatt_stop(void)'):]
        self.assertLess(start.index('bt_gatt_conn_cb_register'),
                        start.index('SIOCBTADVSTART'))
        self.assertIn('bt_gatt_conn_cb_unregister(&g_conn_cb)', start)
        self.assertIn('bt_gatt_conn_cb_unregister(&g_conn_cb)', stop)
        self.assertIn('g_state = g_conn_cb_registered ? NXRS_RADIO_GATT_STOPPING', start)
        self.assertIn('g_state = NXRS_RADIO_GATT_STOPPING;', stop)
        self.assertNotIn('pthread_mutex_lock(&g_lock);\n\n  if (g_conn_cb_registered)', stop)
        device = (PATH.parent / 'src/device.rs').read_text()
        failed_start = device[device.index('fn start_gatt('):device.index('fn wait_gatt_finish(')]
        self.assertIn('stop_gatt(observation)', failed_start)

    def test_disconnect_barrier_waits_for_callback_and_native_idle_before_cleanup(self):
        native = (PATH.parent / 'ffi/gatt.c').read_text()
        wait = native[native.index('static int nxrs_radio_gatt_wait_disconnect(void)'):
                      native.index('int nxrs_radio_gatt_stop(void)')]
        stop = native[native.index('int nxrs_radio_gatt_stop(void)'):]
        source = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <pthread.h>
#include <string.h>
#include <unistd.h>
#define NXRS_RADIO_TIMEOUT_MS 10000
#define NXRS_RADIO_NAME "bnep0"
#define SIOCBTADVSTOP 7
#define NXRS_RADIO_GATT_STOPPED 0
#define NXRS_RADIO_GATT_RUNNING 2
#define NXRS_RADIO_GATT_STOPPING 3
#define clock_gettime fake_clock_gettime
#define nanosleep fake_nanosleep
#define close fake_close
struct btreq_s { char btr_name[16]; };
struct bt_gatt_conn_cb_s { int unused; };
static struct bt_gatt_conn_cb_s g_conn_cb;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_state = NXRS_RADIO_GATT_RUNNING, g_fd = 42;
static int g_conn_cb_registered = 1, g_stop_in_progress;
static int g_snapshot_error, g_ioctl_calls, g_unregister_calls, g_close_calls;
static int g_unregister_error;
static int g_interrupt_sleep;
static int64_t g_now_ms;
static uint32_t g_connected[8], g_disconnected[8], g_native[8];
static uint32_t g_last_connected, g_last_disconnected, g_last_native;
static unsigned g_plan_len, g_probe;
static int fake_clock_gettime(clockid_t clock, struct timespec *now) {
  assert(clock == CLOCK_MONOTONIC);
  now->tv_sec = g_now_ms / 1000;
  now->tv_nsec = (g_now_ms % 1000) * 1000000;
  return 0;
}
static int fake_nanosleep(const struct timespec *delay, struct timespec *remain) {
  assert(delay->tv_sec == 0 && delay->tv_nsec == 10000000);
  (void)remain;
  if (g_interrupt_sleep) {
    g_interrupt_sleep = 0;
    errno = EINTR;
    return -1;
  }
  g_now_ms += 10;
  return 0;
}
static int nxrs_radio_gatt_connections(uint32_t *connected,
                                       uint32_t *disconnected,
                                       uint32_t *native, int32_t *reason) {
  unsigned index;
  if (g_snapshot_error) return g_snapshot_error;
  index = g_probe < g_plan_len ? g_probe : g_plan_len - 1;
  g_probe++;
  *connected = g_last_connected = g_connected[index];
  *disconnected = g_last_disconnected = g_disconnected[index];
  *native = g_last_native = g_native[index];
  *reason = 19;
  return 0;
}
static int nxrs_radio_gatt_ioctl(int fd, int request, struct btreq_s *arg) {
  assert(fd == 42 && request == SIOCBTADVSTOP);
  assert(strcmp(arg->btr_name, NXRS_RADIO_NAME) == 0);
  g_ioctl_calls++;
  return 0;
}
static int bt_gatt_conn_cb_unregister(struct bt_gatt_conn_cb_s *cb) {
  assert(cb == &g_conn_cb && g_conn_cb_registered);
  assert(g_probe > 0 && g_last_connected == g_last_disconnected && g_last_native == 0);
  g_unregister_calls++;
  return g_unregister_error;
}
int fake_close(int fd) {
  assert(fd == 42 && g_probe > 0);
  assert(g_last_connected == g_last_disconnected && g_last_native == 0);
  g_close_calls++;
  return 0;
}
''' + wait + stop + r'''
static void plan(unsigned length, const uint32_t *connected,
                 const uint32_t *disconnected, const uint32_t *native) {
  assert(length > 0 && length <= 8);
  memcpy(g_connected, connected, length * sizeof(uint32_t));
  memcpy(g_disconnected, disconnected, length * sizeof(uint32_t));
  memcpy(g_native, native, length * sizeof(uint32_t));
  g_plan_len = length;
  g_probe = 0;
  g_last_connected = g_last_disconnected = g_last_native = UINT32_MAX;
}
static void assert_owned(int unregister_calls, int close_calls) {
  assert(g_state == NXRS_RADIO_GATT_STOPPING && g_fd == 42);
  assert(g_conn_cb_registered && g_unregister_calls == unregister_calls);
  assert(g_close_calls == close_calls);
}
int main(void) {
  const uint32_t c1[] = {1, 1, 1}, d1[] = {0, 1, 1}, n1[] = {1, 1, 0};
  const uint32_t c2[] = {1, 1}, d2[] = {0, 1}, n2[] = {0, 0};
  const uint32_t busy_c[] = {1}, busy_d[] = {0}, busy_n[] = {1};
  const uint32_t retired[] = {1};
  const uint32_t idle[] = {0};

  /* Callback arrives while the native stack still reports a live link. */
  plan(3, c1, d1, n1);
  assert(nxrs_radio_gatt_stop() == 0);
  assert(g_probe == 3 && g_ioctl_calls == 1);
  assert(g_unregister_calls == 1 && g_close_calls == 1);
  assert(g_state == NXRS_RADIO_GATT_STOPPED && g_fd == -1);

  /* Native idle alone cannot substitute for the delayed disconnect callback. */
  g_state = NXRS_RADIO_GATT_RUNNING; g_fd = 42; g_conn_cb_registered = 1;
  plan(2, c2, d2, n2);
  assert(nxrs_radio_gatt_stop() == 0);
  assert(g_probe == 2 && g_unregister_calls == 2 && g_close_calls == 2);

  /* Timeout preserves ownership; a later stop retries and releases it. */
  g_state = NXRS_RADIO_GATT_RUNNING; g_fd = 42; g_conn_cb_registered = 1;
  g_now_ms = 0;
  g_interrupt_sleep = 1;
  plan(1, busy_c, busy_d, busy_n);
  assert(nxrs_radio_gatt_stop() == ETIMEDOUT);
  assert(g_now_ms == NXRS_RADIO_TIMEOUT_MS && !g_interrupt_sleep);
  assert_owned(2, 2);
  plan(1, retired, retired, idle);
  assert(nxrs_radio_gatt_stop() == 0);
  assert(g_state == NXRS_RADIO_GATT_STOPPED && g_fd == -1);
  assert(!g_conn_cb_registered && g_unregister_calls == 3 && g_close_calls == 3);

  /* A snapshot failure also keeps callback, descriptor, and STOPPING state. */
  g_state = NXRS_RADIO_GATT_RUNNING; g_fd = 42; g_conn_cb_registered = 1;
  plan(1, idle, idle, idle);
  g_snapshot_error = EIO;
  assert(nxrs_radio_gatt_stop() == EIO);
  assert_owned(3, 3);
  g_snapshot_error = 0;
  assert(nxrs_radio_gatt_stop() == 0);
  assert(g_state == NXRS_RADIO_GATT_STOPPED && g_fd == -1);
  assert(g_unregister_calls == 4 && g_close_calls == 4);

  /* Already idle has no artificial wait. */
  g_state = NXRS_RADIO_GATT_RUNNING; g_fd = 42; g_conn_cb_registered = 1;
  plan(1, idle, idle, idle);
  { int64_t before = g_now_ms;
    assert(nxrs_radio_gatt_stop() == 0);
    assert(g_now_ms == before && g_probe == 1);
  }

  /* Once advertising/link are quiet, closing their descriptor is independent
   * of retrying removal of the separately owned static callback. */
  g_state = NXRS_RADIO_GATT_RUNNING; g_fd = 42; g_conn_cb_registered = 1;
  plan(1, retired, retired, idle);
  g_unregister_error = -EIO;
  assert(nxrs_radio_gatt_stop() == EIO);
  assert(g_state == NXRS_RADIO_GATT_STOPPING && g_fd == -1);
  assert(g_conn_cb_registered && g_unregister_calls == 6 && g_close_calls == 6);
  g_unregister_error = 0;
  assert(nxrs_radio_gatt_stop() == 0);
  assert(g_state == NXRS_RADIO_GATT_STOPPED && !g_conn_cb_registered);
  assert(g_unregister_calls == 7 && g_close_calls == 6);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'disconnect.c').write_text(source)
            result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                                     '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                                     str(path / 'disconnect.c'), '-o', str(path / 'disconnect')],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(path / 'disconnect')], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_gatt_sequence_and_all_payload_bytes(self):
        for sequence in (0, 1, 99, 255, 256):
            packet = RADIO.gatt_packet(sequence)
            self.assertEqual(len(packet), 20)
            self.assertEqual(int.from_bytes(packet[:4], 'big'), sequence)
            self.assertEqual(packet[4:], bytes((sequence + index) % 256 for index in range(16)))

    def test_udp_contract_can_run_beyond_original_twenty_packets(self):
        packet = RADIO.WIFI.expected_packet(999)
        self.assertEqual(int.from_bytes(packet[:4], 'big'), 999)
        self.assertEqual(len(packet), 512)
        self.assertEqual(packet[4:], bytes((999 + index) % 256 for index in range(508)))

    def test_udp_failure_diagnostic_contains_no_raw_error_or_credentials(self):
        source = (PATH.parent / 'src/device.rs').read_text()
        messages = re.findall(r'"NXRS_RADIO_TRAFFIC_FAIL ([^"\n]+)"', source)
        self.assertEqual(messages, [
            'phase=connect sequence=0 kind={:?}',
            'phase=send sequence={sequence} kind={:?}',
            'phase=receive sequence={sequence} kind={:?}',
            'phase=shutdown sequence={sequence} kind={:?}',
        ])
        for message in messages:
            self.assertRegex(source, re.escape(f'"NXRS_RADIO_TRAFFIC_FAIL {message}",') + r'\s+error\.kind\(\)')


    def test_native_lifecycle_failures_report_codes_before_primary_error_propagation(self):
        source = (PATH.parent / 'src/device.rs').read_text()
        self.assertEqual(re.findall(r'"NXRS_RADIO_NATIVE_FAIL ([^"\n]+)"', source), [
            'stage=gattstart code={started}',
            'stage=gattfinish code={finished}',
            'stage=gattstop code={stopped}',
        ])
        stopped = source.index('let stopped = stop_gatt(Some(&observation));')
        self.assertLess(stopped, source.index('result?;', stopped))
        self.assertIn('"NXRS_RADIO_NATIVE_FAIL stage=gattstop code={stopped}"', source[source.index('fn stop_gatt('):])

    def test_completed_gatt_uses_disconnect_before_overlap_verdict(self):
        # Behavioral Rust tests cover both overlap outcomes. This guard checks
        # that the native fixture actually uses that tested handoff helper.
        source = (PATH.parent / 'src/device.rs').read_text()
        self.assertIn('super::finish_gatt(observe(Some(&worker), mode), wait_gatt_finish)', source)
        self.assertIn('return Ok(traffic.is_none() || first.is_some_and(|first| current > first + 5));', source)
        self.assertLess(source.index('wait_gatt_finish);', source.index('fn coexist(')),
                        source.index('let stopped = stop_gatt(Some(&observation));'))

    def test_complete_gatt_takes_precedence_over_peer_loss(self):
        # Behavioral Rust tests cover the decision table; check the target
        # loop uses it before triggering traffic or accepting completion.
        source = (PATH.parent / 'src/device.rs').read_text()
        observe = source[source.index('fn observe('):source.index('fn traffic(')]
        complete = observe.index('super::gatt_complete(')
        self.assertLess(complete, observe.index('traffic.begin();'))
        self.assertIn('NXRS_RADIO_GATT_PEER_LOST writes={writes} reads={reads}', observe)
        self.assertIn('let complete = complete?;', observe)
        self.assertIn('let deadline = Instant::now() + Duration::from_secs(60);', observe)




    def test_marker_consumption_preserves_later_bytes(self):
        pending = bytearray(b'noise NXRS_RADIO_GATT_DONE\nnext')
        result = RADIO.until(Mock(), pending, b'NXRS_RADIO_GATT_DONE', b'', 1)
        self.assertTrue(result.endswith(b'NXRS_RADIO_GATT_DONE'))
        self.assertEqual(pending, b'\nnext')

    def test_failure_and_credential_echo_override_pass_marker(self):
        for data in (b'NXRS_RADIO_FAIL error=Timeout\nNXRS_RADIO_PASS',
                     b'password-secret\nNXRS_RADIO_PASS'):
            with self.subTest(data=data):
                with self.assertRaises(RuntimeError) as error:
                    RADIO.until(Mock(), bytearray(data), b'NXRS_RADIO_PASS', b'password-secret', 1)
                self.assertNotIn('password-secret', str(error.exception))

    def test_tcp_echo_handles_fragmented_stream_and_natural_eof(self):
        host, peer = socket.socketpair()
        served, natural_eof, errors = set(), threading.Event(), []

        def worker():
            try:
                RADIO.serve_tcp_connection(host, served, natural_eof)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        expected = RADIO.WIFI.expected_packet(0)
        for part in (expected[:2], expected[2:7], expected[7:]):
            peer.sendall(part)
        echoed = bytearray()
        while len(echoed) < len(expected):
            echoed.extend(peer.recv(len(expected) - len(echoed)))
        self.assertEqual(bytes(echoed), expected)
        peer.shutdown(socket.SHUT_WR)
        RADIO.join_worker(thread, errors)
        host.close()
        peer.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(served, {0})
        self.assertTrue(natural_eof.is_set())

    def test_tcp_rejects_eof_midframe_and_invalid_sequence_or_pattern(self):
        for packet in (b'\x00\x00',
                       (1).to_bytes(4, 'big') + RADIO.WIFI.expected_packet(1)[4:],
                       RADIO.WIFI.expected_packet(0)[:-1] + b'\xff'):
            with self.subTest(packet=packet[:5]):
                host, peer = socket.socketpair()
                served, eof = set(), threading.Event()
                peer.sendall(packet)
                peer.shutdown(socket.SHUT_WR)
                with host:
                    with self.assertRaises(RuntimeError):
                        RADIO.serve_tcp_connection(host, served, eof)
                peer.close()
                self.assertFalse(eof.is_set())
                self.assertEqual(served, set())

    def test_worker_fault_prevents_pass_and_join_is_bounded(self):
        worker = Mock()
        worker.is_alive.return_value = False
        fault = ValueError('bad frame')
        with self.assertRaisesRegex(RuntimeError, 'host radio worker failed') as error:
            RADIO.join_worker(worker, [fault])
        self.assertIs(error.exception.__cause__, fault)
        worker.join.assert_called_once_with(timeout=2)

    def test_forced_socket_cleanup_cannot_turn_a_running_worker_into_pass(self):
        worker, connection = Mock(), Mock()
        worker.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'worker did not stop'):
            RADIO.finish_worker(worker, [], [connection])
        connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        self.assertEqual(worker.join.call_count, 2)
        for call in worker.join.call_args_list:
            self.assertEqual(call.kwargs, {'timeout': 2})

    def test_failed_worker_is_preserved_when_socket_is_already_closed(self):
        worker, connection = Mock(), Mock()
        worker.is_alive.return_value = False
        connection.shutdown.side_effect = OSError('already closed')
        fault = ValueError('bad frame')
        with self.assertRaisesRegex(RuntimeError, 'host radio worker failed') as error:
            RADIO.finish_worker(worker, [fault], [connection])
        self.assertIs(error.exception.__cause__, fault)


if __name__ == '__main__':
    unittest.main()
