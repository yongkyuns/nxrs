#!/usr/bin/env python3
"""Actual SDK camera + Rust gateway: no USB clock/enrollment; synthetic media.

The TCP relay paces an authenticated upload past the30s media reconnect budget.
This tests RAM transfer and joined retirement, not physical flash or MCUboot.
"""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

from tls_reference import private


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait(predicate, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('network enrollment progress deadline')


def run(ingest, camera):
    with tempfile.TemporaryDirectory(prefix='nxrs-network-enrollment-') as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        source_port, relay_port, bootstrap_port = port(), port(), port()
        private(root/'port', str(source_port))
        private(root/'publisher.token', '1'*64+'\n')
        for name, code in [('encoder', 'import sys\nn=0\nwhile data:=sys.stdin.buffer.read(1):\n'
                ' sys.stdout.buffer.write(data);sys.stdout.buffer.flush();n+=1\n'
                ' print("frame="+str(n),file=sys.stderr,flush=True)\n'),
                ('publisher', 'import sys\nassert sys.stdin.buffer.read(64)==b"1"*64\n'
                 'n=0\nwhile data:=sys.stdin.buffer.read(1):\n n+=1;print("packet="+str(n),flush=True)\n')]:
            private(root/name, '#!'+sys.executable+'\n'+code)
            (root/name).chmod(0o700)
        configuration = dict(camera=f'127.0.0.1:{relay_port}', allow_insecure_camera_http=False,
            rtsp='127.0.0.1:8554', stream_path='camera', publisher_user='publisher',
            publisher_token_file=str(root/'publisher.token'), ffmpeg=str(root/'encoder'),
            publisher=str(root/'publisher'), width=96, height=96, frame_timeout_ms=3000,
            bootstrap=dict(listen=f'127.0.0.1:{bootstrap_port}', directory=str(root/'authority'),
                           site_id='site', camera_id='camera', gateway_id='gateway'))
        private(root/'config.json', json.dumps(configuration))
        command = [str(ingest), str(root/'config.json'), '--require-camera-tls']
        subprocess.run([*command, '--init-bootstrap'], check=True, capture_output=True, timeout=10)
        private(root/'commission.bin', (root/'authority/commission.bin').read_bytes())
        subprocess.run([*command, '--check-config'], check=True, capture_output=True, timeout=10)
        processes, logs, threads = [], [], []
        paced, closed = threading.Event(), threading.Event()
        relay = socket.socket()
        relay.bind(('127.0.0.1', relay_port));relay.listen();relay.settimeout(.2)

        def forward(source, target, throttle):
            try:
                while data := source.recv(128 if throttle else 8192):
                    if throttle and paced.is_set():
                        time.sleep(.2)
                    target.sendall(data)
            except OSError:
                pass
            finally:
                for sock in [source, target]:
                    try: sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass

        def accept():
            while not closed.is_set():
                try: client, _ = relay.accept()
                except socket.timeout: continue
                except OSError: return
                try: source = socket.create_connection(('127.0.0.1', source_port), timeout=3)
                except OSError: client.close();continue
                client.settimeout(5);source.settimeout(100)
                for args in [(client, source, True), (source, client, False)]:
                    thread = threading.Thread(target=forward, args=args)
                    threads.append(thread);thread.start()
        acceptor = threading.Thread(target=accept);acceptor.start()

        def start(command, name):
            log = (root/name).open('w+')
            logs.append(log)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            return process

        def stop(process):
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=8) == 0

        def frames():
            try: return json.loads((root/'state.json').read_text())['frames']
            except (FileNotFoundError, json.JSONDecodeError): return 0

        try:
            gateway = start(command, 'gateway.log')
            # A valid TLS pin alone cannot authorize either time or a CSR.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False;context.verify_mode = ssl.CERT_NONE
            context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
            def ready():
                try:
                    with socket.create_connection(('127.0.0.1', bootstrap_port), timeout=.1): return True
                except OSError: return False
            wait(ready)
            with socket.create_connection(('127.0.0.1', bootstrap_port), timeout=3) as raw, context.wrap_socket(raw) as tls:
                assert hashlib.sha256(tls.getpeercert(binary_form=True)).digest() == (root/'commission.bin').read_bytes()[10:42]
                tls.sendall(b'NXB1'+b'\0'*32+b'\0\0')
                try: assert tls.recv(1) == b''
                except (ssl.SSLError, ConnectionResetError): pass
            assert (root/'authority/generation').read_text().strip() == '0'
            device = start([str(camera), str(root), '30000', 'network'], 'camera.log')
            wait(lambda: frames() >= 5)
            baseline = frames()
            # Cross two refresh intervals and the initial lease's expiry.
            time.sleep(32)
            assert device.poll() is None and gateway.poll() is None
            assert frames() > baseline + 20
            stop(gateway)
            gateway = start(command, 'gateway-restarted.log')
            baseline = frames()
            wait(lambda: frames() > baseline + 5)
            # The stopped owner releases its listener and children before upload.
            stop(gateway)
            private(root/'control', 'stop')
            assert device.wait(timeout=8) == 0
            private(root/'control', '')
            private(root/'image.bin', struct.pack('<I', 0x96f3b83d)+bytes(24572))
            paced.set()
            started = time.monotonic()
            gateway = start([*command, '--update', str(root/'image.bin')], 'gateway-update.log')
            # The updater initially has an obsolete grant. A cold camera must
            # enroll its new volatile key without waiting for that transfer.
            time.sleep(.3)
            device = start([str(camera), str(root), '30000', 'network'], 'camera-update.log')
            assert device.wait(timeout=80) == 0, (root/'camera-update.log').read_text()
            assert time.monotonic() - started > 30
            assert 'UPDATE_RECEIVED' in (root/'camera-update.log').read_text()
            wait(lambda: 'Firmware transfer committed' in (root/'gateway-update.log').read_text())
            stop(gateway)
            print('PASS: SDK network enrollment, >30s trusted time, gateway restart, paced OTA and joined cleanup')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=8)
                    except subprocess.TimeoutExpired: process.kill();process.wait()
            closed.set();relay.close();acceptor.join(timeout=3)
            for thread in threads: thread.join(timeout=6)
            assert not acceptor.is_alive() and not any(thread.is_alive() for thread in threads)
            for log in logs: log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ingest', type=Path, required=True)
    parser.add_argument('--camera-source', type=Path, required=True)
    args = parser.parse_args()
    run(args.ingest.resolve(), args.camera_source.resolve())
