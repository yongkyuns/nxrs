#!/usr/bin/env python3
"""Audit clean transport/app builds and independently receive real UDP packets."""
import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('hal_isolation', ROOT / 'tools/check-hal-isolation.py')
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)

def check_packets(packets):
    assert len(packets) == 2, 'missing or extra datagram'
    assert [p['hex'] for p in packets] == ['007f80ff', b'transport-only'.hex()], 'wrong packet bytes/order'
    peers = [p['peer'] for p in packets]
    assert peers[0] == peers[1], 'unexpected sender change'
    assert len(peers[0]) == 2 and peers[0][0] == '127.0.0.1', 'wrong source address'
    assert isinstance(peers[0][1], int) and 0 < peers[0][1] <= 65535, 'wrong source port'


def execute(executable, out):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(5)
        peer = f"127.0.0.1:{receiver.getsockname()[1]}"
        result = subprocess.run([executable, peer], check=True, capture_output=True, text=True, timeout=15)
        assert result.stdout.strip() == 'TRANSPORT_ONLY_PASS'
        packets = []
        for _ in range(2):
            data, source = receiver.recvfrom(65536)
            packets.append({'hex': data.hex(), 'peer': list(source)})
        check_packets(packets)
        receiver.settimeout(0.1)
        try:
            receiver.recvfrom(65536)
        except socket.timeout:
            pass
        else:
            raise AssertionError('unexpected extra datagram')
    (out / 'native-example.log').write_text(result.stdout)
    (out / 'packets.json').write_text(json.dumps(packets, indent=2) + '\n')


def run(out):
    EVIDENCE.run(out, ['transport'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'target/transport-isolation')
    run(parser.parse_args().out)
