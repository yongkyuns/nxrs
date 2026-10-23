"""Qualification-only issuer for a live target CSR; never deployment custody.

The fresh authority and trusted UTC are fixed before compilation. UART supplies
only returned certificates, never a new trust root/time. Private state stays in
an owned disposable directory; reports contain only bounded public digests.
"""
import base64
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('actual_target_issuer', ROOT / 'tools/enrollment/issuer.py')
issuer = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(issuer)
CATALOG = dict(cameras=[dict(camera_id='cam_a', site_id='site_a', gateway_id='gw_a')])
UTC = datetime(2030, 1, 2, tzinfo=issuer.UTC)

def require(value):
    if not value: raise ValueError('target certificate fixture refused')
def digest(data): return hashlib.sha256(data).hexdigest()
def fresh(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output: output.write(data)
def read(directory, name, limit): return issuer.read_file(directory / name, limit)

def public_bindings(directory):
    directory = Path(directory)
    root, pin, clock = (read(directory, name, limit) for name, limit in
                        [('root.der', 2048), ('pin.bin', 32), ('time.bin', 8)])
    require(len(pin) == 32 and len(clock) == 8 and digest(root) == pin.hex())
    require(int.from_bytes(clock, 'little') == int(UTC.timestamp()))
    ca = x509.load_der_x509_certificate(root); issuer.cert_signature(ca, ca)
    require(ca.extensions.get_extension_for_class(x509.BasicConstraints).value == x509.BasicConstraints(True, 0))
    return dict(schema=1, scope='public-certificate-build-fixture-only', qualified=False,
                root_der_sha256=digest(root), pin_bytes_sha256=digest(pin),
                trusted_utc_bytes_sha256=digest(clock), trusted_unix_seconds=int(UTC.timestamp()),
                time_source='private-qualification-fixture-only', deployment_permitted=False)

def prepare(directory):
    directory = Path(directory); directory.mkdir(mode=0o700)
    password = os.urandom(48).hex().encode('ascii')
    pin = issuer.initialize(directory / 'issuer', 'site_a', password, UTC)
    state = issuer.parse((directory / 'issuer' / issuer.STATE).read_bytes())
    root = issuer.certificate(state['ca_certificate']).public_bytes(serialization.Encoding.DER)
    fresh(directory / 'password.bin', password); fresh(directory / 'root.der', root)
    fresh(directory / 'pin.bin', bytes.fromhex(pin))
    fresh(directory / 'time.bin', int(UTC.timestamp()).to_bytes(8, 'little'))
    return public_bindings(directory)

def csr_key(raw):
    require(len(raw) <= 2048)
    request = x509.load_pem_x509_csr(raw)
    require(request.is_signature_valid and request.signature_hash_algorithm.name == 'sha256')
    require(isinstance(request.public_key(), ec.EllipticCurvePublicKey)
            and isinstance(request.public_key().curve, ec.SECP256R1))
    require(request.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == 'cam_A')
    return request.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

def issue_response(directory, label, raw):
    directory = Path(directory); require(label in ('A', 'B')); csr_key(raw)
    bindings = public_bindings(directory); pin = bindings['root_der_sha256']
    password = read(directory, 'password.bin', 96); require(len(password) == 96)
    authority = directory / 'issuer'
    before = issuer.parse((authority / issuer.STATE).read_bytes())
    require(len(before['entries']) == (0 if label == 'A' else 2))
    require(not any((directory / (prefix + '-' + label + suffix)).exists() for prefix,suffix in
                    [('csr','.pem'),('leaf','.der'),('wrong','.der'),('short','.der')]))
    if label == 'B': require((directory / 'csr-A.pem').is_file())
    serial = issuer.issue(authority, pin, password, raw, CATALOG, 'cam_a', 'camera', '127.0.0.1', now=UTC)
    state = issuer.parse((authority / issuer.STATE).read_bytes())
    leaf = issuer.certificate(next(entry['certificate'] for entry in state['entries'] if entry['serial'] == serial))
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    wrong_csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'cam_A')])).sign(wrong_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM))
    wrong_serial = issuer.issue(authority, pin, password, wrong_csr, CATALOG, 'cam_a', 'camera', '127.0.0.1', now=UTC)
    state = issuer.parse((authority / issuer.STATE).read_bytes())
    wrong = issuer.certificate(next(entry['certificate'] for entry in state['entries'] if entry['serial'] == wrong_serial))
    ca = issuer.certificate(state['ca_certificate']); signing_key = issuer.issuer_key(state, ca, password)
    # This short-lived signed control uses the actual CSR key and authority;
    # production issuance still has its ordinary 1..30-day validity policy.
    builder = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
        .public_key(leaf.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(UTC - timedelta(minutes=5)).not_valid_after(UTC + timedelta(seconds=10)))
    for extension in leaf.extensions: builder = builder.add_extension(extension.value, extension.critical)
    short = builder.sign(signing_key, hashes.SHA256())
    snapshot = digest((authority / issuer.STATE).read_bytes())
    request = x509.load_pem_x509_csr(raw); bad = bytearray(request.public_bytes(serialization.Encoding.DER)); bad[-1] ^= 1
    encoded = base64.b64encode(bad)
    corrupted = b'-----BEGIN CERTIFICATE REQUEST-----\n' + b'\n'.join(encoded[i:i+64] for i in range(0,len(encoded),64)) + b'\n-----END CERTIFICATE REQUEST-----\n'
    for rejected in (raw, corrupted):
        refused = False
        try: issuer.issue(authority, pin, password, rejected, CATALOG, 'cam_a', 'camera', '127.0.0.1', now=UTC)
        except ValueError: refused = True
        require(refused and digest((authority / issuer.STATE).read_bytes()) == snapshot)
    responses = [certificate.public_bytes(serialization.Encoding.DER) for certificate in (leaf,wrong,short)]
    require(all(0 < len(value) <= 2048 for value in responses))
    fresh(directory / ('csr-' + label + '.pem'), raw)
    for name, value in zip(('leaf','wrong','short'), responses): fresh(directory / (name + '-' + label + '.der'), value)
    return responses

def verify_execution(directory, requests):
    directory = Path(directory); require(len(requests) == 2)
    bindings = public_bindings(directory); ca = x509.load_der_x509_certificate(read(directory, 'root.der', 2048))
    state = issuer.parse((directory / 'issuer' / issuer.STATE).read_bytes())
    issuer.ca_validate(state, bindings['root_der_sha256'], UTC); issuer.entries_validate(state, ca)
    require(len(state['entries']) == 4)
    keys, certificates = [], []
    for label, raw in zip(('A','B'), requests):
        require(read(directory, 'csr-' + label + '.pem', 2048) == raw); key = csr_key(raw); keys.append(digest(key))
        for name in ('leaf','wrong','short'):
            data = read(directory, name + '-' + label + '.der', 2048)
            certificate = x509.load_der_x509_certificate(data); issuer.cert_signature(certificate, ca)
            public = certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            require((public == key) == (name != 'wrong'))
            require(certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.UniformResourceIdentifier) == ['urn:nxrs:camera:site_a:cam_a'])
            if name == 'leaf': certificates.append(digest(data))
    require(len(set(keys)) == 2)
    return dict(qualified=True, independently_verified_csrs=2, unique_public_keys=True,
        actual_issuer_accepted=True, catalog_identity_enforced=True, repeated_key_refused=True,
        bad_signature_refused=True, public_key_sha256=keys, csr_sha256=[digest(raw) for raw in requests],
        returned_certificate_sha256=certificates, precompiled_public_fixture=bindings,
        correctly_signed_wrong_key_controls=2, short_lifetime_controls=2, short_lifetime_seconds=10,
        protected_generation_qualified=False, hardware_entropy_qualified=False, deployment_permitted=False)

class Interaction:
    def __init__(self, directory): self.directory = Path(directory); public_bindings(self.directory)
    def __call__(self, invocation, write, read_until):
        require(invocation == 'identity_sdk'); write((invocation + '\n').encode('ascii'))
        for label in ('A','B'):
            text = read_until(('NXRS_IDENTITY_CERTIFICATE_READY ' + label + '\n').encode('ascii'))
            blocks = re.findall(r'^NXRS_IDENTITY_CSR_BEGIN ' + label + r'\n(.*?)^NXRS_IDENTITY_CSR_END ' + label + r'$',
                                text.replace('\r',''), re.M | re.S)
            require(len(blocks) == 1)
            for response in issue_response(self.directory, label, blocks[0].encode('ascii')):
                write(('NXRS_CERT_DER ' + str(len(response)) + '\n').encode('ascii'))
                for offset in range(0,len(response),32): write(response[offset:offset+32].hex().encode('ascii') + b'\n')
