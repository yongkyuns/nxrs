"""Bridge logic with C stand-ins; actual native headers are checked in builds."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'nuttx-std/radio/ffi/stats.c'


class RadioStatsTests(unittest.TestCase):
    def test_tcp_observer_is_started_and_stopped_without_udp_sequence_queries(self):
        # Compile the actual NuttX-only Rust routing against host FFI stand-ins.
        # Native witnesses and board builds separately validate the C/ABI side.
        source = SOURCE.parent.parent / 'src/stats.rs'
        program = '#![allow(dead_code)]\n#[path=' + json.dumps(str(source)) + '] mod stats;\n' + r'''
use std::ffi::c_char;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::*};
static ACTIVE: AtomicBool = AtomicBool::new(false);
static UNAVAILABLE: AtomicBool = AtomicBool::new(false);
static STARTS: AtomicUsize = AtomicUsize::new(0);
static STOPS: AtomicUsize = AtomicUsize::new(0);
static PACKET_QUERIES: AtomicUsize = AtomicUsize::new(0);

unsafe fn clear(values: *mut u32, count: usize) {
    unsafe { std::slice::from_raw_parts_mut(values, count) }.fill(0);
}
#[unsafe(no_mangle)]
extern "C" fn nxrs_radio_trace_start(_: *const c_char, _: *const u8, port: u16) -> i32 {
    assert_eq!(port, 4242);
    if UNAVAILABLE.load(SeqCst) { return -38; }
    assert!(!ACTIVE.swap(true, SeqCst));
    STARTS.fetch_add(1, SeqCst);
    0
}
#[unsafe(no_mangle)]
extern "C" fn nxrs_radio_trace_stop() {
    assert!(ACTIVE.swap(false, SeqCst));
    STOPS.fetch_add(1, SeqCst);
}
#[unsafe(no_mangle)]
unsafe extern "C" fn nxrs_radio_trace_snapshot(_: u32, values: *mut u32, count: usize) -> i32 {
    assert!(ACTIVE.load(SeqCst));
    assert_eq!(count, 25);
    PACKET_QUERIES.fetch_add(1, SeqCst);
    unsafe { clear(values, count) };
    0
}
macro_rules! snapshot {
    ($name:ident, $count:expr, $index:expr, $value:expr) => {
        #[unsafe(no_mangle)]
        unsafe extern "C" fn $name(_: *const c_char, values: *mut u32, count: usize) -> i32 {
            assert_eq!(count, $count);
            unsafe { clear(values, count); *values.add($index) = $value; }
            0
        }
    }
}
snapshot!(nxrs_radio_net_snapshot, 12, 0, 16);
snapshot!(nxrs_radio_interface_snapshot, 3, 0, 1);
snapshot!(nxrs_radio_controller_snapshot, 30, 0, 0);
#[unsafe(no_mangle)]
unsafe extern "C" fn nxrs_radio_rx_snapshot(values: *mut u32, count: usize) -> i32 {
    if !ACTIVE.load(SeqCst) { return -38; }
    assert_eq!(count, 16);
    unsafe { clear(values, count) };
    0
}
#[unsafe(no_mangle)]
unsafe extern "C" fn nxrs_radio_daemon_snapshot(values: *mut u32, count: usize) -> i32 {
    if !ACTIVE.load(SeqCst) { return -38; }
    assert_eq!(count, 19);
    unsafe { clear(values, count); *values.add(1) = 10_000_000; }
    0
}
fn main() {
    let peer = "127.0.0.1:4242".parse().unwrap();
    for (index, datagram) in [false, true].into_iter().enumerate() {
        let counters = stats::Counters::start(peer, datagram);
        assert_eq!(STARTS.load(SeqCst), index + 1);
        counters.report_trace(0, "final");
        assert_eq!(PACKET_QUERIES.load(SeqCst), usize::from(datagram));
        drop(counters);
        assert_eq!(STOPS.load(SeqCst), index + 1);
        assert!(!ACTIVE.load(SeqCst));
    }
    UNAVAILABLE.store(true, SeqCst);
    let counters = stats::Counters::start(peer, false);
    counters.report_trace(0, "final");
    drop(counters);
    assert_eq!(STARTS.load(SeqCst), 2);
    assert_eq!(STOPS.load(SeqCst), 2);
    assert_eq!(PACKET_QUERIES.load(SeqCst), 1);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'probe.rs').write_text(program)
            compiled = subprocess.run([
                'rustc', '--edition=2024', '-Aexplicit_builtin_cfgs_in_flags',
                '--cfg', 'target_os="nuttx"', str(root / 'probe.rs'),
                '-o', str(root / 'probe')], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(root / 'probe')], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('NXRS_RADIO_PACKET_TRACE stage=final sequence=0 available=0', result.stdout)
            self.assertIn('NXRS_RADIO_PACKET_TRACE stage=final sequence=0 available=1', result.stdout)
            self.assertIn('NXRS_RADIO_DAEMON stage=final available=1', result.stdout)

    def compile_and_run(self, stats_enabled, interface_enabled, program):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            include = root / 'nuttx/net'
            include.mkdir(parents=True)
            config = []
            if stats_enabled:
                config.extend(['#define CONFIG_NET_STATISTICS 1',
                               '#define CONFIG_NETDEV_STATISTICS 1'])
            if interface_enabled:
                config.extend(['#define CONFIG_NET 1', '#define CONFIG_NET_IPv4 1'])
            (root / 'nuttx/config.h').write_text('\n'.join(config))
            if interface_enabled:
                (root / 'net').mkdir()
                (root / 'net/if.h').write_text('''
#define IFF_UP 0x1
#define IFF_RUNNING 0x2
#define IFF_IS_UP(f) (((f) & IFF_UP) != 0)
#define IFF_IS_RUNNING(f) (((f) & IFF_RUNNING) != 0)
''')
            (include / 'net.h').write_text('''
int net_lock(void);
void net_unlock(void);
struct net_driver_s;
struct net_driver_s *netdev_findbyname(const char *);
''')
            (include / 'netdev.h').write_text('''
#include <stdint.h>
struct net_driver_s {
  uint32_t d_flags;
  uint32_t d_ipaddr;
  struct { uint32_t tx_packets, rx_ipv4, rx_dropped; } d_statistics;
};
''')
            (include / 'netstats.h').write_text('''
#include <stdint.h>
typedef uint16_t net_stats_t;
struct net_stats_s {
  struct { net_stats_t sent, recv, drop, chkerr; } udp;
  struct { net_stats_t rexmit, drop, chkerr; } tcp;
  struct { net_stats_t drop; } ipv4;
};
extern struct net_stats_s g_netstats;
''')
            harness = root / 'stats.c'
            harness.write_text('#include <assert.h>\n#include <string.h>\n#include "' + str(SOURCE) + '"\n' + program)
            binary = root / 'stats'
            result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                     '-I', str(root), str(harness), '-o', str(binary)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_disabled_counters_report_unavailable_and_validate_capacity(self):
        self.compile_and_run(False, False, '''
int main(void) {
  uint32_t values[32] = {123};
  assert(nxrs_radio_net_snapshot("wlan0", values, 12) == -ENOSYS);
  assert(nxrs_radio_interface_snapshot("wlan0", values, 3) == -ENOSYS);
  const uint8_t peer[4] = {192, 0, 2, 1};
  assert(nxrs_radio_trace_start("wlan0", peer, 4444) == -ENOSYS);
  assert(nxrs_radio_trace_snapshot(0, values, 25) == -ENOSYS);
  assert(nxrs_radio_trace_start(NULL, peer, 4444) == -EINVAL);
  assert(nxrs_radio_trace_start("wlan0", NULL, 4444) == -EINVAL);
  assert(nxrs_radio_trace_start("wlan0", peer, 0) == -EINVAL);
  assert(nxrs_radio_trace_snapshot(0, NULL, 25) == -EINVAL);
  assert(nxrs_radio_trace_snapshot(0, values, 24) == -EINVAL);
  assert(nxrs_radio_controller_snapshot("wlan0", values, 30) == -ENOSYS);
  assert(nxrs_radio_controller_snapshot(NULL, values, 30) == -EINVAL);
  assert(nxrs_radio_controller_snapshot("wlan0", NULL, 30) == -EINVAL);
  assert(nxrs_radio_controller_snapshot("wlan0", values, 29) == -EINVAL);
  assert(nxrs_radio_daemon_snapshot(values, 19) == -ENOSYS);
  assert(nxrs_radio_daemon_snapshot(NULL, 19) == -EINVAL);
  assert(nxrs_radio_daemon_snapshot(values, 18) == -EINVAL);
  assert(nxrs_radio_rx_snapshot(values, 16) == -ENOSYS);
  assert(nxrs_radio_rx_snapshot(NULL, 16) == -EINVAL);
  assert(nxrs_radio_rx_snapshot(values, 15) == -EINVAL);
  nxrs_radio_trace_stop();
  assert(values[0] == 123);
  assert(nxrs_radio_net_snapshot(NULL, values, 12) == -EINVAL);
  assert(nxrs_radio_net_snapshot("wlan0", NULL, 12) == -EINVAL);
  assert(nxrs_radio_net_snapshot("wlan0", values, 11) == -EINVAL);
  assert(nxrs_radio_interface_snapshot(NULL, values, 3) == -EINVAL);
  assert(nxrs_radio_interface_snapshot("wlan0", NULL, 3) == -EINVAL);
  assert(nxrs_radio_interface_snapshot("wlan0", values, 2) == -EINVAL);
  assert(nxrs_radio_interface_snapshot("wlan0", values, 4) == -EINVAL);
  return 0;
}
''')

    def test_interface_snapshot_is_available_without_statistics_and_is_read_only(self):
        self.compile_and_run(False, True, '''
static struct net_driver_s device;
static int locks, unlocks, lock_error, missing, lookups;
int net_lock(void) { locks++; return lock_error; }
void net_unlock(void) { unlocks++; }
struct net_driver_s *netdev_findbyname(const char *name) {
  assert(strcmp(name, "wlan0") == 0);
  lookups++;
  return missing ? NULL : &device;
}
int main(void) {
  uint32_t values[3] = {91, 92, 93};
  device.d_flags = 3;
  device.d_ipaddr = 0x01020304;
  assert(nxrs_radio_interface_snapshot("wlan0", values, 3) == 0);
  assert(values[0] == 1 && values[1] == 1 && values[2] == 0x01020304);
  assert(locks == 1 && unlocks == 1 && lookups == 1);
  assert(device.d_flags == 3 && device.d_ipaddr == 0x01020304);
  device.d_flags = 1;
  device.d_ipaddr = 0;
  assert(nxrs_radio_interface_snapshot("wlan0", values, 3) == 0);
  assert(values[0] == 1 && values[1] == 0 && values[2] == 0);
  assert(locks == 2 && unlocks == 2);
  missing = 1; values[0] = 91; values[1] = 92; values[2] = 93;
  assert(nxrs_radio_interface_snapshot("wlan0", values, 3) == -ENODEV);
  assert(locks == 3 && unlocks == 3 && lookups == 3);
  assert(values[0] == 91 && values[1] == 92 && values[2] == 93);
  lock_error = -EBUSY;
  assert(nxrs_radio_interface_snapshot("wlan0", values, 3) == -EBUSY);
  assert(locks == 4 && unlocks == 3 && lookups == 3);
  assert(values[0] == 91 && values[1] == 92 && values[2] == 93);
  assert(nxrs_radio_interface_snapshot("wlan0", values, 4) == -EINVAL);
  assert(locks == 4 && unlocks == 3);
  return 0;
}
''')

    def test_enabled_snapshot_preserves_counters_and_balances_network_lock(self):
        self.compile_and_run(True, True, '''
#include <string.h>
struct net_stats_s g_netstats;
static struct net_driver_s device;
static int locks, unlocks, lock_error, missing;
int net_lock(void) { locks++; return lock_error; }
void net_unlock(void) { unlocks++; }
struct net_driver_s *netdev_findbyname(const char *name) {
  assert(strcmp(name, "wlan0") == 0);
  return missing ? NULL : &device;
}
int main(void) {
  uint32_t values[12];
  g_netstats.udp.sent = 7; g_netstats.udp.recv = 8;
  g_netstats.udp.drop = 9; g_netstats.udp.chkerr = 10;
  g_netstats.tcp.rexmit = 11; g_netstats.tcp.drop = 12;
  g_netstats.tcp.chkerr = 13; g_netstats.ipv4.drop = 14;
  device.d_statistics.tx_packets = 15;
  device.d_statistics.rx_ipv4 = 16; device.d_statistics.rx_dropped = 17;
  assert(nxrs_radio_net_snapshot("wlan0", values, 12) == 0);
  assert(values[0] == 16);
  for (unsigned i = 1; i < 12; i++) assert(values[i] == i + 6);
  assert(locks == 1 && unlocks == 1 && g_netstats.udp.sent == 7);
  missing = 1; values[0] = 123;
  assert(nxrs_radio_net_snapshot("wlan0", values, 12) == -ENODEV);
  assert(locks == 2 && unlocks == 2 && values[0] == 123);
  lock_error = -EBUSY;
  assert(nxrs_radio_net_snapshot("wlan0", values, 12) == -EBUSY);
  assert(locks == 3 && unlocks == 2 && values[0] == 123);
  assert(nxrs_radio_net_snapshot("wlan0", values, 13) == -EINVAL);
  assert(locks == 3 && unlocks == 2);
  return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
