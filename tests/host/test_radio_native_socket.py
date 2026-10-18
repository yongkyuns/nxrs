"""Exercise the radio socket FFI against real loopback sockets."""
import ctypes
import errno
import os
from pathlib import Path
import select
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/nuttx-std/radio/ffi/socket.c"


class NativeRadioSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        library = Path(cls.temporary.name) / "libnxrs_radio_tcp.so"
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra",
            "-Werror", "-fPIC", "-shared", str(SOURCE), "-o", str(library),
        ], check=True)
        cls.ffi = ctypes.CDLL(str(library))
        cls.ffi.nxrs_radio_tcp_open.argtypes = [ctypes.POINTER(ctypes.c_ubyte),
                                                 ctypes.c_uint16]
        cls.ffi.nxrs_radio_tcp_open.restype = ctypes.c_int
        cls.ffi.nxrs_radio_tcp_transfer.argtypes = [ctypes.c_int,
                                                     ctypes.POINTER(ctypes.c_ubyte),
                                                     ctypes.c_size_t,
                                                     ctypes.c_int]
        cls.ffi.nxrs_radio_tcp_transfer.restype = ctypes.c_int
        cls.ffi.nxrs_radio_tcp_finish.argtypes = [ctypes.c_int]
        cls.ffi.nxrs_radio_tcp_finish.restype = ctypes.c_int
        cls.ffi.nxrs_radio_socket_close.argtypes = [ctypes.c_int]
        cls.ffi.nxrs_radio_socket_close.restype = ctypes.c_int
        cls.ffi.nxrs_radio_socket_snapshot.argtypes = [
            ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
        ]
        cls.ffi.nxrs_radio_socket_snapshot.restype = ctypes.c_int
        cls.ffi.nxrs_radio_tcp_state.argtypes = [
            ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
        ]
        cls.ffi.nxrs_radio_tcp_state.restype = ctypes.c_int
        cls.ffi.nxrs_radio_udp_open.argtypes = [ctypes.POINTER(ctypes.c_ubyte),
                                                ctypes.c_uint16]
        cls.ffi.nxrs_radio_udp_open.restype = ctypes.c_int
        cls.ffi.nxrs_radio_udp_send.argtypes = [ctypes.c_int,
                                                ctypes.POINTER(ctypes.c_ubyte),
                                                ctypes.c_size_t]
        cls.ffi.nxrs_radio_udp_send.restype = ctypes.c_int
        cls.ffi.nxrs_radio_udp_receive.argtypes = [ctypes.c_int,
                                                   ctypes.POINTER(ctypes.c_ubyte),
                                                   ctypes.c_size_t]
        cls.ffi.nxrs_radio_udp_receive.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def open(cls, port):
        address = (ctypes.c_ubyte * 4)(127, 0, 0, 1)
        return cls.ffi.nxrs_radio_tcp_open(address, port)

    @classmethod
    def transfer(cls, fd, data, sending):
        buffer_type = ctypes.c_ubyte * max(1, len(data))
        buffer = buffer_type(*data) if data else buffer_type()
        result = cls.ffi.nxrs_radio_tcp_transfer(fd, buffer, len(data), sending)
        return result, bytes(buffer[:len(data)])

    @classmethod
    def snapshot(cls, fd):
        values = (ctypes.c_uint32 * 5)()
        result = cls.ffi.nxrs_radio_socket_snapshot(fd, values, len(values))
        return result, tuple(values)

    def server(self, handler):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        errors = []

        def run():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(3)
                    handler(connection)
            except BaseException as error:  # Relay background failures to the test.
                errors.append(error)
            finally:
                listener.close()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return listener.getsockname()[1], worker, errors

    @staticmethod
    def join_server(worker, errors):
        worker.join(3)
        if worker.is_alive():
            raise AssertionError("loopback server did not finish")
        if errors:
            raise errors[0]

    def test_exact_transfer_sizes(self):
        for size in (16, 64, 256, 512):
            payload = bytes((index * 37 + size) & 0xff for index in range(size))

            def echo(connection):
                incoming = bytearray()
                while len(incoming) < size:
                    incoming.extend(connection.recv(size - len(incoming)))
                connection.sendall(incoming)
                connection.shutdown(socket.SHUT_WR)
                while connection.recv(1024):
                    pass

            port, worker, errors = self.server(echo)
            fd = self.open(port)
            self.assertGreaterEqual(fd, 0)
            self.assertFalse(os.get_inheritable(fd))
            try:
                result, _ = self.transfer(fd, payload, 1)
                self.assertEqual(result, size)
                result, received = self.transfer(fd, bytes(size), 0)
                self.assertEqual((result, received), (size, payload))
                self.assertEqual(self.ffi.nxrs_radio_tcp_finish(fd), 0)
            finally:
                self.assertEqual(self.ffi.nxrs_radio_socket_close(fd), 0)
            with self.subTest(size=size):
                self.join_server(worker, errors)

    def test_fragmented_receive_is_completed(self):
        payload = b"fragmented TCP reply"

        def fragmented(connection):
            for byte in payload:
                connection.sendall(bytes((byte,)))
                time.sleep(0.002)
            connection.shutdown(socket.SHUT_WR)
            while connection.recv(128):
                pass

        port, worker, errors = self.server(fragmented)
        fd = self.open(port)
        try:
            result, received = self.transfer(fd, bytes(len(payload)), 0)
            self.assertEqual((result, received), (len(payload), payload))
            self.assertEqual(self.ffi.nxrs_radio_tcp_finish(fd), 0)
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_eof_and_partial_eof(self):
        for payload, expected in ((b"", 0), (b"abc", -errno.ECONNRESET)):
            def close_after_payload(connection, content=payload):
                connection.sendall(content)
                connection.shutdown(socket.SHUT_WR)
                while connection.recv(128):
                    pass

            port, worker, errors = self.server(close_after_payload)
            fd = self.open(port)
            try:
                result, _ = self.transfer(fd, bytes(8), 0)
                self.assertEqual(result, expected)
                self.assertEqual(self.ffi.nxrs_radio_tcp_finish(fd), 0)
            finally:
                self.ffi.nxrs_radio_socket_close(fd)
            self.join_server(worker, errors)

    def test_finish_rejects_trailing_peer_data(self):
        def send_after_eof(connection):
            self.assertEqual(connection.recv(1), b"")
            connection.sendall(b"x")
            connection.shutdown(socket.SHUT_WR)

        port, worker, errors = self.server(send_after_eof)
        fd = self.open(port)
        try:
            self.assertEqual(self.ffi.nxrs_radio_tcp_finish(fd), -errno.EPROTO)
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_finish_wait_is_bounded_and_keeps_fd_owned_by_caller(self):
        port, worker, errors = self.server(lambda connection: time.sleep(2.4))
        fd = self.open(port)
        started = time.monotonic()
        self.assertEqual(self.ffi.nxrs_radio_tcp_finish(fd), -errno.ETIMEDOUT)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.7)
        self.assertLess(elapsed, 2.5)
        os.fstat(fd)
        self.assertEqual(self.ffi.nxrs_radio_socket_close(fd), 0)
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.join_server(worker, errors)

    def test_refused_connect_and_argument_bounds(self):
        reservation = socket.socket()
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        reservation.close()
        self.assertLess(self.open(port), 0)
        self.assertEqual(self.ffi.nxrs_radio_tcp_transfer(-1, None, 0, 0),
                         -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_tcp_transfer(0, None, 514, 0),
                         -errno.EINVAL)

    def test_tcp_state_host_fallback_preserves_output(self):
        values = (ctypes.c_uint32 * 8)(*([123] * 8))
        self.assertEqual(self.ffi.nxrs_radio_tcp_state(-1, values, 8), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_tcp_state(0, None, 8), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_tcp_state(0, values, 7), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_tcp_state(0, values, 8), -errno.ENOSYS)
        self.assertEqual(tuple(values), (123,) * 8)

    def test_tcp_state_native_lock_and_descriptor_lifetime(self):
        # Stand-ins exercise ownership/error paths; the board build compiles
        # the same bridge against the actual configured TCP layout.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            headers = {
                'nuttx/config.h': '#define CONFIG_NET_TCP 1\n#define NET_TCP_HAVE_STACK 1\n',
                'nuttx/fs/fs.h': 'struct file { int refs; };\nvoid file_put(struct file *);\n',
                'nuttx/net/net.h': '''
struct sock_intf_s { int native; };
struct socket { int s_type, s_domain; void *s_conn; const struct sock_intf_s *s_sockif; };
int sockfd_socket(int, struct file **, struct socket **);
''',
                'net/inet/inet.h': '''
const struct sock_intf_s *inet_sockif(int, int, int);
''',
                'net/utils/utils.h': '''
void conn_dev_lock(struct socket_conn_s *, struct net_driver_s *);
void conn_dev_unlock(struct socket_conn_s *, struct net_driver_s *);
''',
                'net/tcp/tcp.h': '''
#define TCP_STATE_MASK 0x0f
struct socket_conn_s { int marker; };
struct net_driver_s { int marker; };
struct tcp_conn_s {
  struct socket_conn_s sconn;
  struct net_driver_s *dev;
  uint8_t tcpstateflags, rto, timer, nrtx, sa, sv;
  uint32_t tx_unacked, snd_wnd;
};
''',
            }
            for name, content in headers.items():
                header = root / name
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_text(content)
            harness = root / 'tcp_state.c'
            harness.write_text('#include <assert.h>\n#define __NuttX__ 1\n'
                               f'#include "{SOURCE}"\n' + r'''
static struct file owned_file = {1};
static struct net_driver_s device;
static struct tcp_conn_s connection = {.dev=&device, .tcpstateflags=0x14,
  .rto=7, .timer=5, .nrtx=2, .sa=8, .sv=4, .tx_unacked=512, .snd_wnd=4096};
static const struct sock_intf_s native_if = {1}, offload_if = {2};
static struct socket owned_socket = {SOCK_STREAM, AF_INET, &connection, &native_if};
static int lookup_error, interface_unavailable, lookups, puts, locks, unlocks, held;
int sockfd_socket(int fd, struct file **filep, struct socket **socketp)
{
  assert(fd == 9);
  lookups++;
  if (lookup_error) return lookup_error;
  owned_file.refs++;
  *filep = &owned_file;
  *socketp = &owned_socket;
  return 0;
}
void file_put(struct file *filep)
{
  assert(filep == &owned_file && owned_file.refs == 2 && !held);
  owned_file.refs--;
  puts++;
}
const struct sock_intf_s *inet_sockif(int domain, int type, int protocol)
{
  assert(owned_file.refs == 2);
  assert((domain == AF_INET || domain == AF_INET6) && type == SOCK_STREAM);
  assert(protocol == IPPROTO_TCP);
  return interface_unavailable ? NULL : &native_if;
}
void conn_dev_lock(struct socket_conn_s *conn, struct net_driver_s *dev)
{
  locks++;
  assert(conn == &connection.sconn && dev == &device);
  assert(owned_file.refs == 2 && !held);
  held = 1;
}
void conn_dev_unlock(struct socket_conn_s *conn, struct net_driver_s *dev)
{
  assert(held && conn == &connection.sconn && dev == &device);
  held = 0;
  unlocks++;
}
int main(void)
{
  uint32_t values[8] = {123};
  assert(nxrs_radio_tcp_state(-1, values, 8) == -EINVAL);
  assert(nxrs_radio_tcp_state(9, NULL, 8) == -EINVAL);
  assert(nxrs_radio_tcp_state(9, values, 7) == -EINVAL);
  assert(lookups == 0 && puts == 0 && locks == 0);

  lookup_error = -EBADF;
  assert(nxrs_radio_tcp_state(9, values, 8) == -EBADF);
  assert(puts == 0 && locks == 0 && values[0] == 123);
  lookup_error = 0;

  owned_socket.s_type = SOCK_DGRAM;
  assert(nxrs_radio_tcp_state(9, values, 8) == -ENOTCONN);
  owned_socket.s_type = SOCK_STREAM;
  owned_socket.s_domain = AF_UNIX;
  assert(nxrs_radio_tcp_state(9, values, 8) == -ENOTCONN);
  owned_socket.s_domain = AF_INET;
  owned_socket.s_conn = NULL;
  assert(nxrs_radio_tcp_state(9, values, 8) == -ENOTCONN);
  assert(values[0] == 123);
  owned_socket.s_conn = &connection;
  owned_socket.s_sockif = &offload_if;
  assert(nxrs_radio_tcp_state(9, values, 8) == -ENOSYS);
  owned_socket.s_sockif = &native_if;
  interface_unavailable = 1;
  assert(nxrs_radio_tcp_state(9, values, 8) == -ENOSYS);
  interface_unavailable = 0;
  assert(values[0] == 123 && locks == 0);

  for (int i = 0; i < 2; i++)
    {
      owned_socket.s_domain = i ? AF_INET6 : AF_INET;
      assert(nxrs_radio_tcp_state(9, values, 8) == 0);
      uint32_t expected[8] = {4, 7, 5, 2, 512, 4096, 8, 4};
      assert(memcmp(values, expected, sizeof(values)) == 0);
      assert(connection.tcpstateflags == 0x14 && connection.timer == 5);
    }
  assert(owned_file.refs == 1 && puts == 7 && locks == 2 && unlocks == 2);
  return 0;
}
''')
            binary = root / 'tcp_state'
            result = subprocess.run([
                os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                '-I', str(root), str(harness), '-o', str(binary),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_partial_send_eintr_and_deadline_witness(self):
        harness = Path(self.temporary.name) / "tcp_fake_syscalls.c"
        binary = Path(self.temporary.name) / "tcp_fake_syscalls"
        harness.write_text(f'''#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>

static int clock_calls, send_calls, option_calls, deadline_mode;
static const unsigned char *first_buffer;

int fake_clock_gettime(clockid_t clock_id, struct timespec *value)
{{
  if (clock_id != CLOCK_MONOTONIC) return -1;
  value->tv_sec = deadline_mode && clock_calls >= 2 ? 102 : 100;
  value->tv_nsec = 0;
  clock_calls++;
  return 0;
}}

int fake_setsockopt(int fd, int level, int option, const void *value,
                    socklen_t length)
{{
  const struct timeval *timeout = value;
  if (fd != 9 || level != SOL_SOCKET || option != SO_SNDTIMEO ||
      length != sizeof(*timeout) || timeout->tv_sec < 0 ||
      timeout->tv_usec < 0 || timeout->tv_usec >= 1000000) return -1;
  option_calls++;
  return 0;
}}

ssize_t fake_send(int fd, const void *value, size_t length, int flags)
{{
  const unsigned char *bytes = value;
  int call = send_calls++;
  if (fd != 9 || flags != MSG_NOSIGNAL || length == 0) return -1;
  if (call == 0) first_buffer = bytes;
  if (!deadline_mode && call == 0) {{ errno = EINTR; return -1; }}
  if (deadline_mode) return 1;
  if (call == 1 && bytes != first_buffer) return -1;
  if (call == 2 && (bytes != first_buffer + 1 || bytes[0] != 0x22))
    return -1;
  return 1;
}}

#define clock_gettime fake_clock_gettime
#define setsockopt fake_setsockopt
#define send fake_send
#include "{SOURCE}"
#undef clock_gettime
#undef setsockopt
#undef send

int main(void)
{{
  unsigned char bytes[2] = {{0x11, 0x22}};
  int result = nxrs_radio_tcp_transfer(9, bytes, sizeof(bytes), 1);
  if (result != 2 || send_calls != 3 || option_calls != 3) return 1;

  clock_calls = send_calls = option_calls = 0;
  deadline_mode = 1;
  result = nxrs_radio_tcp_transfer(9, bytes, sizeof(bytes), 1);
  if (result != -ETIMEDOUT || send_calls != 1 || option_calls != 1) return 2;
  return 0;
}}
''')
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra",
            "-Werror", str(harness), "-o", str(binary),
        ], check=True)
        subprocess.run([str(binary)], check=True)

    @classmethod
    def open_udp(cls, port):
        address = (ctypes.c_ubyte * 4)(127, 0, 0, 1)
        return cls.ffi.nxrs_radio_udp_open(address, port)

    @classmethod
    def udp_send(cls, fd, data):
        buffer_type = ctypes.c_ubyte * max(1, len(data))
        buffer = buffer_type(*data) if data else buffer_type()
        return cls.ffi.nxrs_radio_udp_send(fd, buffer, len(data))

    @classmethod
    def udp_receive(cls, fd, capacity=513):
        buffer_type = ctypes.c_ubyte * max(1, capacity)
        buffer = buffer_type()
        result = cls.ffi.nxrs_radio_udp_receive(fd, buffer, capacity)
        return result, bytes(buffer[:max(result, 0)])

    def udp_server(self, handler):
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(3)
        errors = []

        def run():
            try:
                handler(peer)
            except BaseException as error:
                errors.append(error)
            finally:
                peer.close()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return peer.getsockname()[1], worker, errors

    def test_udp_exact_echo_sizes_and_owned_fd_cleanup(self):
        for size in (16, 64, 256, 512):
            payload = bytes((index * 19 + size) & 0xff for index in range(size))

            def echo(peer):
                incoming, address = peer.recvfrom(1024)
                peer.sendto(incoming, address)

            port, worker, errors = self.udp_server(echo)
            fd = self.open_udp(port)
            self.assertGreaterEqual(fd, 0)
            self.assertFalse(os.get_inheritable(fd))
            try:
                self.assertEqual(self.udp_send(fd, payload), size)
                self.assertEqual(self.udp_receive(fd), (size, payload))
            finally:
                self.assertEqual(self.ffi.nxrs_radio_socket_close(fd), 0)
            with self.subTest(size=size):
                with self.assertRaises(OSError):
                    os.fstat(fd)
                self.join_server(worker, errors)

    def test_udp_datagram_boundaries_and_513_byte_capacity(self):
        payloads = (b"first", b"", b"second datagram",
                    bytes(range(256)) * 2 + b"x")

        def echo_datagrams(peer):
            for _ in payloads:
                incoming, address = peer.recvfrom(1024)
                peer.sendto(incoming, address)

        port, worker, errors = self.udp_server(echo_datagrams)
        fd = self.open_udp(port)
        try:
            for index, payload in enumerate(payloads):
                with self.subTest(length=len(payload)):
                    if index == 1:
                        self.assertEqual(
                            self.ffi.nxrs_radio_udp_send(fd, None, 0), 0)
                    else:
                        self.assertEqual(self.udp_send(fd, payload), len(payload))
                    self.assertEqual(self.udp_receive(fd), (len(payload), payload))
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_udp_oversized_incoming_datagram_is_truncated_to_513(self):
        payload = bytes(index & 0xff for index in range(600))

        def send_oversized(peer):
            _, address = peer.recvfrom(16)
            peer.sendto(payload, address)

        port, worker, errors = self.udp_server(send_oversized)
        fd = self.open_udp(port)
        try:
            self.assertEqual(self.udp_send(fd, b"ready"), 5)
            self.assertEqual(self.udp_receive(fd), (513, payload[:513]))
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_udp_timeout_is_bounded_and_preserves_would_block(self):
        port, worker, errors = self.udp_server(lambda peer: time.sleep(2.4))
        fd = self.open_udp(port)
        started = time.monotonic()
        try:
            result, _ = self.udp_receive(fd, 1)
            elapsed = time.monotonic() - started
            self.assertIn(result, (-errno.EAGAIN, -errno.EWOULDBLOCK))
            self.assertGreaterEqual(elapsed, 1.7)
            self.assertLess(elapsed, 2.5)
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_snapshot_udp_peeks_without_consuming_datagram(self):
        payload = b"\x12\x34\x56\x78remaining payload"

        def reply(peer):
            _, address = peer.recvfrom(32)
            peer.sendto(payload, address)

        port, worker, errors = self.udp_server(reply)
        fd = self.open_udp(port)
        try:
            self.assertEqual(self.udp_send(fd, b"ready"), 5)
            # FIONREAD and MSG_PEEK are separate observations, not an atomic
            # snapshot: arrival between them may legitimately report 0 then 4.
            self.assertEqual(select.select([fd], [], [], 1)[0], [fd])
            result = self.snapshot(fd)
            self.assertEqual(result[0], 0)
            self.assertGreaterEqual(result[1][0], len(payload))
            self.assertEqual(result[1][1:], (0, 4, 0, 0x12345678))
            repeated = self.snapshot(fd)
            self.assertGreaterEqual(repeated[1][0], len(payload))
            self.assertEqual(repeated[1][1:], result[1][1:])
            self.assertEqual(self.udp_receive(fd), (len(payload), payload))
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_snapshot_empty_udp_is_nonblocking(self):
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        port = peer.getsockname()[1]
        fd = self.open_udp(port)
        try:
            started = time.monotonic()
            result, empty = self.snapshot(fd)
            self.assertEqual(result, 0)
            self.assertEqual(empty[0], 0)
            self.assertEqual(empty[1], 0)
            self.assertEqual(empty[2], 0)
            self.assertIn(empty[3], (errno.EAGAIN, errno.EWOULDBLOCK))
            self.assertEqual(empty[4], 0)
            self.assertLess(time.monotonic() - started, 0.5)
            os.fstat(fd)
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
            peer.close()

    def test_snapshot_zero_length_udp_datagram_is_queued(self):
        sent = threading.Event()

        def send_empty(peer):
            _, address = peer.recvfrom(32)
            peer.sendto(b"", address)
            sent.set()

        port, worker, errors = self.udp_server(send_empty)
        fd = self.open_udp(port)
        try:
            self.assertEqual(self.udp_send(fd, b"ready"), 5)
            self.assertTrue(sent.wait(1))
            # A zero-byte peek succeeds for the queued empty datagram. Receiving
            # zero again proves the snapshot did not consume it.
            snapshot = self.snapshot(fd)
            self.assertEqual(snapshot[0], 0)
            self.assertEqual(snapshot[1][1:], (0, 0, 0, 0))
            self.assertEqual(self.udp_receive(fd), (0, b""))
            result, drained = self.snapshot(fd)
            self.assertEqual(result, 0)
            self.assertIn(drained[3], (errno.EAGAIN, errno.EWOULDBLOCK))
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_snapshot_tcp_data_and_eof(self):
        payload = b"\x89\xab\xcd\xeftail"

        def send_then_eof(connection):
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            while connection.recv(128):
                pass

        port, worker, errors = self.server(send_then_eof)
        fd = self.open(port)
        try:
            expected = (len(payload), 0, 4, 0, 0x89ABCDEF)
            deadline = time.monotonic() + 1
            while True:
                result = self.snapshot(fd)
                if result[1][2] == 4 or time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
            self.assertEqual(result, (0, expected))
            self.assertEqual(self.transfer(fd, bytes(len(payload)), 0),
                             (len(payload), payload))
            self.assertEqual(self.snapshot(fd), (0, (0, 0, 0, 0, 0)))
        finally:
            self.ffi.nxrs_radio_socket_close(fd)
        self.join_server(worker, errors)

    def test_snapshot_argument_and_probe_failures_keep_descriptor_owned(self):
        values = (ctypes.c_uint32 * 5)()
        self.assertEqual(self.ffi.nxrs_radio_socket_snapshot(-1, values, 5),
                         -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_socket_snapshot(0, None, 5),
                         -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_socket_snapshot(0, values, 4),
                         -errno.EINVAL)

        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        fd = self.open_udp(peer.getsockname()[1])
        self.assertGreaterEqual(fd, 0)
        self.assertEqual(self.ffi.nxrs_radio_socket_close(fd), 0)
        with self.assertRaises(OSError):
            os.fstat(fd)
        values[:] = [0xFFFFFFFF] * 5
        self.assertEqual(self.ffi.nxrs_radio_socket_snapshot(fd, values, 5), 0)
        self.assertEqual(tuple(values), (0, errno.EBADF, 0, errno.EBADF, 0))
        peer.close()

    def test_udp_argument_bounds_and_single_syscall_errors(self):
        self.assertEqual(self.ffi.nxrs_radio_udp_send(-1, None, 0), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_udp_send(0, None, 1), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_udp_send(0, None, 514), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_udp_receive(-1, None, 0), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_udp_receive(0, None, 1), -errno.EINVAL)
        self.assertEqual(self.ffi.nxrs_radio_udp_receive(0, None, 514), -errno.EINVAL)

        harness = Path(self.temporary.name) / "udp_single_syscall.c"
        binary = Path(self.temporary.name) / "udp_single_syscall"
        harness.write_text(f'''#include <errno.h>
#include <stddef.h>
#include <sys/socket.h>
static int calls;
static ssize_t fake_send(int fd, const void *data, size_t length, int flags)
{{ (void)fd; (void)data; (void)length; (void)flags; calls++; errno = EINTR; return -1; }}
static ssize_t fake_recv(int fd, void *data, size_t length, int flags)
{{ (void)fd; (void)data; (void)length; (void)flags; calls++; errno = EAGAIN; return -1; }}
#define send fake_send
#define recv fake_recv
#include "{SOURCE}"
int main(void)
{{
  unsigned char byte = 0;
  int sent = nxrs_radio_udp_send(9, &byte, 1);
  if (sent != -EINTR || calls != 1) return 1;
  calls = 0;
  int received = nxrs_radio_udp_receive(9, &byte, 1);
  return received != -EAGAIN || calls != 1;
}}
''')
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra",
            "-Werror", str(harness), "-o", str(binary),
        ], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
