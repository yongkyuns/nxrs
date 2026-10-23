"""Disposable actual-issuer certificate controls for a live SDK-owned CSR.

Only this qualification fixture unlocks its own newly created test authority.
It never touches production credentials or prints certificates/private keys.
"""
import hashlib
import importlib.util
import ipaddress
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('certificate_test_issuer', ROOT / 'tools/enrollment/issuer.py')
issuer = importlib.util.module_from_spec(spec); spec.loader.exec_module(issuer)

def main():
    folder = Path(sys.argv[1]); csr = (folder / 'request.csr').read_bytes()
    assert folder.is_dir() and len(csr) <= 4096
    # Deliberately different from the host clock: the actual SDK must use the
    # trusted observation, or even the valid certificate is not yet valid.
    now = issuer.instant(datetime(2030, 1, 2, tzinfo=issuer.UTC)); password = secrets.token_hex(48).encode()
    authority = folder / 'issuer'; pin = issuer.initialize(authority, 'site_a', password, now)
    catalog = dict(cameras=[dict(camera_id='cam_a', site_id='site_a', gateway_id='gw_a')])
    issuer.issue(authority, pin, password, csr, catalog, 'cam_a', 'camera', '127.0.0.1', now=now)
    state = issuer.parse((authority / issuer.STATE).read_bytes()); root = issuer.certificate(state['ca_certificate'])
    leaf = issuer.certificate(state['entries'][0]['certificate']); key = issuer.issuer_key(state, root, password)
    def write(name, certificate):
        raw = certificate.public_bytes(serialization.Encoding.DER)
        assert len(raw) <= 2048
        (folder / (name + '.der')).write_bytes(raw)
    write('root', root); write('valid', leaf)
    (folder / 'pin.bin').write_bytes(bytes.fromhex(pin)); (folder / 'now').write_text(str(int(now.timestamp())))
    def variant(name, *, changes=None, public=None, start=None, end=None, algorithm=None):
        builder = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
                   .public_key(public or leaf.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(start or leaf.not_valid_before_utc).not_valid_after(end or leaf.not_valid_after_utc))
        changes = changes or {}
        for extension in leaf.extensions:
            changed = changes.pop(extension.oid, (extension.value, extension.critical))
            if changed is not None: builder = builder.add_extension(*changed)
        for value, critical in changes.values(): builder = builder.add_extension(value, critical)
        write(name, builder.sign(key, algorithm or hashes.SHA256()))
    san = x509.SubjectAlternativeName.oid; eku = x509.ExtendedKeyUsage.oid
    uri = x509.UniformResourceIdentifier('urn:nxrs:camera:site_a:cam_a'); ip = x509.IPAddress(ipaddress.IPv4Address('127.0.0.1'))
    for name, names in [('wrong-uri', [x509.UniformResourceIdentifier('urn:nxrs:camera:site_a:other'), ip]),
                        ('wrong-ip', [uri, x509.IPAddress(ipaddress.IPv4Address('127.0.0.2'))]),
                        ('extra-san', [uri, ip, x509.DNSName('unexpected.invalid')]),
                        ('duplicate-uri', [uri, uri, ip]), ('missing-ip', [uri])]:
        variant(name, changes={san: (x509.SubjectAlternativeName(names), False)})
    variant('client-role', changes={eku: (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)})
    variant('extra-role', changes={eku: (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), False)})
    variant('missing-role', changes={eku: None})
    variant('missing-san', changes={san: None})
    variant('ca-leaf', changes={x509.BasicConstraints.oid: (x509.BasicConstraints(True, 0), True)})
    variant('wrong-usage', changes={x509.KeyUsage.oid: (issuer.usage(True), True)})
    variant('missing-usage', changes={x509.KeyUsage.oid: None})
    variant('wrong-key', public=ec.generate_private_key(ec.SECP256R1()).public_key())
    variant('expired', start=now-timedelta(days=2), end=now-timedelta(seconds=1))
    variant('future', start=now+timedelta(seconds=1))
    variant('too-long', end=now+timedelta(days=31))
    variant('short', end=now+timedelta(seconds=1))
    variant('sha384', algorithm=hashes.SHA384())
    raw=bytearray(leaf.public_bytes(serialization.Encoding.DER)); raw[-1]^=1
    (folder/'bad-signature.der').write_bytes(raw)
    raw=leaf.public_bytes(serialization.Encoding.DER)
    (folder/'trailing.der').write_bytes(raw+b'\0'); (folder/'two-certificates.der').write_bytes(raw+raw)
    (folder/'truncated.der').write_bytes(raw[:-1])
    assert hashlib.sha256((folder/'root.der').read_bytes()).hexdigest()==pin
    print('Disposable actual-issuer certificate controls prepared')
if __name__=='__main__': main()
