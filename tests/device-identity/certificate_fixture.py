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
    # Actual durable issuer exports provide the admission, refresh and revoked
    # generations. All other CRLs are explicitly disposable negative controls.
    def export_crl(name):
        output = folder / ('bundle-' + name)
        issuer.export(authority, pin, password, output, now=now)
        crl = x509.load_pem_x509_crl((output / 'revocations.pem').read_bytes())
        (folder / (name + '.crl')).write_bytes(crl.public_bytes(serialization.Encoding.DER))
        return crl
    clear = export_crl('clear')
    newer = export_crl('newer')
    issuer.revoke(authority, pin, password, format(leaf.serial_number, 'x'), now=now)
    revoked = export_crl('revoked')
    generation = clear.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
    assert generation == 3 and newer.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 4
    assert revoked.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 6
    aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(root.public_key())
    def crl_variant(name, *, number=generation, start=now, end=now+timedelta(hours=24),
                    authority_key=aki, signer=key, issuer_name=root.subject, serials=(), extra=None,
                    entry_extension=False, algorithm=None):
        builder = x509.CertificateRevocationListBuilder().issuer_name(issuer_name).last_update(start).next_update(end)
        if number is not None: builder = builder.add_extension(x509.CRLNumber(number), False)
        builder = builder.add_extension(authority_key, False)
        if extra is not None: builder = builder.add_extension(extra, False)
        for serial in serials:
            entry = x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(start)
            if entry_extension: entry = entry.add_extension(x509.CRLReason(x509.ReasonFlags.key_compromise), False)
            builder = builder.add_revoked_certificate(entry.build())
        raw = builder.sign(signer, algorithm or hashes.SHA256()).public_bytes(serialization.Encoding.DER)
        (folder / (name + '.crl')).write_bytes(raw)
        return raw
    # Re-signing the exact same TBS must be idempotent despite a new ECDSA signature.
    resigned = crl_variant('resigned')
    assert x509.load_der_x509_crl(resigned).tbs_certlist_bytes == clear.tbs_certlist_bytes
    assert resigned != clear.public_bytes(serialization.Encoding.DER)
    crl_variant('conflict', serials=(leaf.serial_number,))
    crl_variant('removed', number=7)
    crl_variant('time-regression', number=7, start=now-timedelta(seconds=1),
                end=now+timedelta(hours=23), serials=(leaf.serial_number,))
    crl_variant('short', end=now+timedelta(seconds=1))
    crl_variant('wrong-signer', signer=ec.generate_private_key(ec.SECP256R1()))
    crl_variant('wrong-aki', authority_key=x509.AuthorityKeyIdentifier(b'\0'*20, None, None))
    crl_variant('wrong-issuer', issuer_name=leaf.subject)
    crl_variant('future', start=now+timedelta(seconds=1))
    crl_variant('expired', start=now-timedelta(days=1), end=now)
    crl_variant('old', start=now-timedelta(days=1, seconds=1), end=now+timedelta(seconds=1))
    crl_variant('long', end=now+timedelta(days=1, seconds=1))
    crl_variant('zero', number=0)
    crl_variant('huge-number', number=1_000_000_001)
    crl_variant('missing-number', number=None)
    crl_variant('delta', extra=x509.DeltaCRLIndicator(1))
    crl_variant('unknown', extra=x509.UnrecognizedExtension(x509.ObjectIdentifier('1.2.3.4'), b'\x05\0'))
    crl_variant('entry-extension', serials=(leaf.serial_number,), entry_extension=True)
    crl_variant('duplicate-serial', serials=(leaf.serial_number, leaf.serial_number))
    crl_variant('at-limit', serials=range(1, 513))
    crl_variant('too-many', serials=range(1, 514))
    crl_variant('sha384', algorithm=hashes.SHA384())
    raw = bytearray(clear.public_bytes(serialization.Encoding.DER)); raw[-1] ^= 1
    (folder / 'bad-signature.crl').write_bytes(raw)
    raw = clear.public_bytes(serialization.Encoding.DER)
    (folder / 'trailing.crl').write_bytes(raw + b'\0')
    (folder / 'chained.crl').write_bytes(raw + raw)
    assert hashlib.sha256((folder/'root.der').read_bytes()).hexdigest()==pin
    print('Disposable actual-issuer certificate controls prepared')
if __name__=='__main__': main()
