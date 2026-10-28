"""Fetch the hash-pinned release explicitly; Cargo never performs network access."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def fetch(output):
    pin = json.loads(Path(__file__).with_name('source.json').read_text())
    if output.exists():
        data = output.read_bytes()
    else:
        with urlopen(pin['url'], timeout=60) as response:
            data = response.read(pin['size']+1)
    if len(data) != pin['size'] or hashlib.sha256(data).hexdigest() != pin['sha256']:
        raise ValueError('TLS source release size/checksum mismatch')
    if not output.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as destination:
            destination.write(data)
    print('Verified Mbed TLS '+pin['version']+' / TF-PSA-Crypto '+pin['tf_psa_crypto'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'target/mbedtls-4.2.0.tar.bz2')
    fetch(parser.parse_args().out)
