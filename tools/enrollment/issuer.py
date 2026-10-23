#!/usr/bin/env python3
"""Offline nxrs device CSR issuer. Does not generate endpoint keys or install trust."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import getpass
import ipaddress
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import stat
import sys
import warnings

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, SignatureAlgorithmOID

UTC = timezone.utc
MAX_STATE = 2 * 1024 * 1024
MAX_ENTRIES = 512
STATE = 'issuer.json'
PENDING = '.issuer-next'


def require(condition):
    if not condition:
        raise ValueError('enrollment validation failed')


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value))
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def parse(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def exact(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys.split()))


def instant(now=None):
    value = now if now is not None else datetime.now(UTC)
    require(value.tzinfo is not None)
    return value.astimezone(UTC).replace(microsecond=0)


def private_stat(fd, directory=False, limit=MAX_STATE):
    info = os.fstat(fd)
    require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if not directory:
        require(info.st_nlink == 1 and info.st_size <= limit)


def read_file(path, limit, parent_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        private_stat(fd, limit=limit)
        result = bytearray()
        while len(result) <= limit:
            piece = os.read(fd, min(65536, limit + 1 - len(result)))
            if not piece:
                break
            result.extend(piece)
        require(len(result) <= limit)
        private_stat(fd, limit=limit)
        return bytes(result)
    finally:
        os.close(fd)


def new_file(parent_fd, name, data):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        private_stat(fd, limit=MAX_STATE)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            require(written > 0)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def absolute(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts)
    return path


@contextmanager
def locked(directory):
    fd = os.open(absolute(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        private_stat(fd, directory=True)
        lock_fd = os.open('.issuer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=fd)
        private_stat(lock_fd, limit=0)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        private_stat(lock_fd, limit=0)
        yield fd
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(fd)


def password_value(value):
    require(isinstance(value, bytes) and 32 <= len(value) <= 1024 and b'\n' not in value and b'\0' not in value)
    return value


def prompt_password(prompt):
    # getpass can otherwise fall back to echoed input on an unusable terminal.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt).encode()


def read_password(fd):
    if fd is None:
        require(sys.stdin.isatty())
        return password_value(prompt_password('Offline issuer passphrase: '))
    require(fd >= 3)
    try:
        os.set_inheritable(fd, False)
        private_stat(fd, limit=1025)
        return password_value(os.pread(fd, 1026, 0).removesuffix(b'\n'))
    finally:
        os.close(fd)


def p256(key):
    require(isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1))
    return key


def spki(key):
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def one_pem(data, label, limit=16384):
    if isinstance(data, str):
        data = data.encode()
    require(len(data) <= limit and re.fullmatch(
        rb'-----BEGIN ' + label + rb'-----\r?\n[A-Za-z0-9+/=\r\n]+-----END ' + label + rb'-----\s*', data))
    return data


def certificate(data):
    return x509.load_pem_x509_certificate(one_pem(data, b'CERTIFICATE'))


def signed_bytes(data):
    purpose = b'issuer-ledger' if 'encrypted_key' in data else b'public-bundle'
    return b'nxrs-device-enrollment-v1/' + purpose + b'\0' + canonical(data)


def signature(key, data):
    return key.sign(signed_bytes(data), ec.ECDSA(hashes.SHA256())).hex()


def signed(data, key):
    return dict(data, signature=signature(key, data))


def verify_signed(data, key):
    require(isinstance(data['signature'], str) and re.fullmatch(r'[0-9a-f]{16,160}', data['signature']))
    body = {k: v for k, v in data.items() if k != 'signature'}
    key.verify(bytes.fromhex(data['signature']), signed_bytes(body), ec.ECDSA(hashes.SHA256()))


def cert_signature(cert, issuer):
    require(cert.signature_algorithm_oid == SignatureAlgorithmOID.ECDSA_WITH_SHA256)
    p256(issuer.public_key()).verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))


def usage(ca):
    return x509.KeyUsage(True, False, False, False, False, ca, ca, False, False)


def leaf_extensions(site, entry, key, ca):
    if entry['role'] == 'camera':
        require(isinstance(entry['ip'], str))
        address = ipaddress.IPv4Address(entry['ip'])
        require(str(address) == entry['ip'])
        require(any(address in ipaddress.IPv4Network(network) for network in
                    ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8']))
        uri = f"urn:nxrs:camera:{site}:{entry['camera_id']}"
        names = [x509.UniformResourceIdentifier(uri), x509.IPAddress(address)]
        eku = ExtendedKeyUsageOID.SERVER_AUTH
    else:
        require(entry['role'] == 'ingest' and entry['ip'] is None)
        uri = f"urn:nxrs:ingest:{site}:{entry['gateway_id']}:{entry['camera_id']}"
        names = [x509.UniformResourceIdentifier(uri)]
        eku = ExtendedKeyUsageOID.CLIENT_AUTH
    return [(x509.BasicConstraints(False, None), True), (usage(False), True),
            (x509.ExtendedKeyUsage([eku]), False), (x509.SubjectAlternativeName(names), False),
            (x509.SubjectKeyIdentifier.from_public_key(key), False),
            (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), False)]


def extensions_equal(cert, expected):
    require({e.oid: (e.value, e.critical) for e in cert.extensions} ==
            {value.oid: (value, critical) for value, critical in expected})


def ca_validate(data, expected, now):
    identifier(data['site_id'])
    require(type(data['version']) is int and data['version'] == 1)
    require(type(data['generation']) is int and 1 <= data['generation'] <= 1_000_000_000)
    require(type(data['updated_at']) is int and 0 < data['updated_at'] <= int(now.timestamp()) + 300)
    require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected))
    ca = certificate(data['ca_certificate'])
    require(ca.fingerprint(hashes.SHA256()).hex() == expected and ca.issuer == ca.subject)
    require(ca.not_valid_before_utc <= now < ca.not_valid_after_utc)
    require(ca.subject == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'nxrs device enrollment CA'),
                                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, data['site_id'])]))
    cert_signature(ca, ca)
    extensions_equal(ca, [(x509.BasicConstraints(True, 0), True), (usage(True), True),
                         (x509.SubjectKeyIdentifier.from_public_key(ca.public_key()), False)])
    verify_signed(data, ca.public_key())
    return ca


def entries_validate(data, ca):
    entries = data['entries']
    require(isinstance(entries, list) and len(entries) <= MAX_ENTRIES)
    keys, serials = {spki(ca.public_key())}, {format(ca.serial_number, 'x')}
    for entry in entries:
        exact(entry, 'serial role camera_id gateway_id ip issued_at revoked_at certificate')
        identifier(entry['camera_id']); identifier(entry['gateway_id'])
        require(type(entry['issued_at']) is int and 0 < entry['issued_at'] <= data['updated_at'])
        require(entry['revoked_at'] is None or (type(entry['revoked_at']) is int and
                entry['issued_at'] <= entry['revoked_at'] <= data['updated_at']))
        cert = certificate(entry['certificate'])
        require(entry['serial'] == format(cert.serial_number, 'x') and entry['serial'] not in serials)
        require(cert.issuer == ca.subject and ca.not_valid_before_utc <= cert.not_valid_before_utc
                and cert.not_valid_after_utc <= ca.not_valid_after_utc)
        issued = datetime.fromtimestamp(entry['issued_at'], UTC)
        require(cert.not_valid_before_utc == issued - timedelta(minutes=5))
        require(timedelta(days=1) <= cert.not_valid_after_utc - issued <= timedelta(days=30))
        key = p256(cert.public_key())
        require(spki(key) not in keys)
        cert_signature(cert, ca)
        extensions_equal(cert, leaf_extensions(data['site_id'], entry, key, ca))
        subject = entry['camera_id'] if entry['role'] == 'camera' else entry['gateway_id']
        require(cert.subject == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        keys.add(spki(key)); serials.add(entry['serial'])
    return keys


def load(parent_fd, expected, now, pending=False):
    if not pending:
        try:
            os.stat(PENDING, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('pending issuer update requires recovery')
    data = parse(read_file(PENDING if pending else STATE, MAX_STATE, parent_fd))
    exact(data, 'version site_id generation updated_at ca_certificate encrypted_key entries signature')
    require(isinstance(data['encrypted_key'], str) and data['encrypted_key'].startswith('-----BEGIN ENCRYPTED PRIVATE KEY-----\n'))
    ca = ca_validate(data, expected, now)
    entries_validate(data, ca)
    return data, ca


def issuer_key(data, ca, password):
    key = serialization.load_pem_private_key(data['encrypted_key'].encode(), password_value(password))
    require(isinstance(key, ec.EllipticCurvePrivateKey) and spki(p256(key.public_key())) == spki(ca.public_key()))
    return key


def save(parent_fd, data, key):
    complete = signed({k: v for k, v in data.items() if k != 'signature'}, key)
    expected = certificate(complete['ca_certificate']).fingerprint(hashes.SHA256()).hex()
    ca = ca_validate(complete, expected, datetime.fromtimestamp(complete['updated_at'], UTC))
    entries_validate(complete, ca)
    payload = canonical(complete) + b'\n'
    require(len(payload) <= MAX_STATE)
    new_file(parent_fd, PENDING, payload)
    os.fsync(parent_fd)  # Persist the completed pending ledger before replacement.
    os.replace(PENDING, STATE, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.fsync(parent_fd)


def sync_parent(path):
    # Ancestors are trusted; a trusted ancestor may itself be a symlink.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize(directory, site, password, now=None):
    site = identifier(site); password_value(password); now = instant(now)
    directory = absolute(directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    with locked(directory) as fd:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'nxrs device enrollment CA'),
                          x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, site)])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
              .not_valid_after(now + timedelta(days=730)).add_extension(x509.BasicConstraints(True, 0), True)
              .add_extension(usage(True), True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False).sign(key, hashes.SHA256()))
        data = dict(version=1, site_id=site, generation=1, updated_at=int(now.timestamp()),
                    ca_certificate=pem(ca), entries=[], encrypted_key=key.private_bytes(
                        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.BestAvailableEncryption(password)).decode())
        save(fd, data, key)
    sync_parent(directory)
    return ca.fingerprint(hashes.SHA256()).hex()


def scope(catalog, camera, site):
    identifier(camera)
    require(isinstance(catalog, dict) and isinstance(catalog.get('cameras'), list)
            and 1 <= len(catalog['cameras']) <= 64)
    found, ids = None, set()
    for item in catalog['cameras']:
        require(isinstance(item, dict))
        camera_id = identifier(item['camera_id'])
        identifier(item['gateway_id']); identifier(item['site_id'])
        require(camera_id not in ids)
        ids.add(camera_id)
        if camera_id == camera:
            require(item['site_id'] == site)
            found = item
    require(found is not None)
    return found['gateway_id']


def issue(directory, expected, password, csr_data, catalog, camera, role, ip=None, days=7, now=None):
    now = instant(now)
    require(type(days) is int and 1 <= days <= 30 and role in ['camera', 'ingest'])
    csr = x509.load_pem_x509_csr(one_pem(csr_data, b'CERTIFICATE REQUEST'))
    key = p256(csr.public_key())
    require(csr.is_signature_valid and csr.signature_algorithm_oid == SignatureAlgorithmOID.ECDSA_WITH_SHA256)
    with locked(directory) as fd:
        data, ca = load(fd, expected, now)
        require(now.timestamp() >= data['updated_at'] and len(data['entries']) < MAX_ENTRIES)
        require(spki(key) not in entries_validate(data, ca))
        gateway = scope(catalog, camera, data['site_id'])
        entry = dict(serial='', role=role, camera_id=camera, gateway_id=gateway, ip=ip,
                     issued_at=int(now.timestamp()), revoked_at=None, certificate='')
        extensions = leaf_extensions(data['site_id'], entry, key, ca)
        require(now + timedelta(days=days) <= ca.not_valid_after_utc)
        ca_key = issuer_key(data, ca, password)
        name = camera if role == 'camera' else gateway
        builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                   .issuer_name(ca.subject).public_key(key).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days)))
        for value, critical in extensions:
            builder = builder.add_extension(value, critical)
        cert = builder.sign(ca_key, hashes.SHA256())
        entry.update(serial=format(cert.serial_number, 'x'), certificate=pem(cert))
        data['entries'].append(entry)
        data.update(generation=data['generation'] + 1, updated_at=int(now.timestamp()))
        save(fd, data, ca_key)
        return entry['serial']


def revoke(directory, expected, password, serial, now=None):
    now = instant(now)
    with locked(directory) as fd:
        data, ca = load(fd, expected, now)
        require(now.timestamp() >= data['updated_at'])
        key = issuer_key(data, ca, password)
        found = next((e for e in data['entries'] if e['serial'] == serial), None)
        require(found is not None)
        if found['revoked_at'] is None:
            found['revoked_at'] = int(now.timestamp())
            data.update(generation=data['generation'] + 1, updated_at=int(now.timestamp()))
            save(fd, data, key)


def export(directory, expected, password, output, now=None):
    now = instant(now)
    with locked(directory) as fd:
        data, ca = load(fd, expected, now)
        key = issuer_key(data, ca, password)
        require(now.timestamp() >= data['updated_at'])
        # Reserve a fresh CRL sequence durably before signing or publishing it.
        data.update(generation=data['generation'] + 1, updated_at=int(now.timestamp()))
        save(fd, data, key)
        deadline = min(now + timedelta(hours=24), ca.not_valid_after_utc)
        builder = x509.CertificateRevocationListBuilder().issuer_name(ca.subject).last_update(now).next_update(deadline)
        builder = builder.add_extension(x509.CRLNumber(data['generation']), False)
        builder = builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), False)
        for entry in data['entries']:
            if entry['revoked_at'] is not None:
                builder = builder.add_revoked_certificate(x509.RevokedCertificateBuilder()
                    .serial_number(int(entry['serial'], 16))
                    .revocation_date(datetime.fromtimestamp(entry['revoked_at'], UTC)).build())
        crl = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        body = {k: data[k] for k in ['version', 'site_id', 'generation', 'updated_at', 'ca_certificate', 'entries']}
        body['revocations'] = crl.decode()
        bundle = canonical(signed(body, key)) + b'\n'
        verify_bundle(bundle, expected, data['generation'], now)
        output = absolute(output)
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        out_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            private_stat(out_fd, directory=True)
            new_file(out_fd, '.incomplete', b'')
            new_file(out_fd, 'ca.pem', data['ca_certificate'].encode())
            new_file(out_fd, 'revocations.pem', crl)
            for entry in data['entries']:
                new_file(out_fd, 'cert-' + entry['serial'] + '.pem', entry['certificate'].encode())
            new_file(out_fd, 'bundle.json', bundle)
            os.fsync(out_fd)
            os.unlink('.incomplete', dir_fd=out_fd)
            os.fsync(out_fd)
        finally:
            os.close(out_fd)
        sync_parent(output)


def verify_bundle(payload, expected, minimum_generation, now=None):
    now = instant(now)
    require(len(payload) <= MAX_STATE)
    data = parse(payload)
    exact(data, 'version site_id generation updated_at ca_certificate entries revocations signature')
    ca = ca_validate(data, expected, now)
    entries_validate(data, ca)
    require(type(minimum_generation) is int and 1 <= minimum_generation <= data['generation'])
    crl = x509.load_pem_x509_crl(one_pem(data['revocations'], b'X509 CRL', 65536))
    require(crl.issuer == ca.subject and crl.signature_algorithm_oid == SignatureAlgorithmOID.ECDSA_WITH_SHA256
            and crl.is_signature_valid(ca.public_key()))
    require(now - timedelta(hours=24) <= crl.last_update_utc <= now + timedelta(minutes=5)
            and now < crl.next_update_utc <= min(crl.last_update_utc + timedelta(hours=24), ca.not_valid_after_utc))
    require(int(crl.last_update_utc.timestamp()) >= data['updated_at'])
    require({e.oid: (e.value, e.critical) for e in crl.extensions} == {
        x509.CRLNumber.oid: (x509.CRLNumber(data['generation']), False),
        x509.AuthorityKeyIdentifier.oid: (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), False)})
    require(len(crl) == sum(e['revoked_at'] is not None for e in data['entries']))
    require({r.serial_number: int(r.revocation_date_utc.timestamp()) for r in crl} ==
            {int(e['serial'], 16): e['revoked_at'] for e in data['entries'] if e['revoked_at'] is not None})
    return data


def recover(directory, expected, now=None):
    """Complete only a fully signed, monotonic pending ledger; never discard it."""
    now = instant(now)
    with locked(directory) as fd:
        pending, _ = load(fd, expected, now, pending=True)
        current = parse(read_file(STATE, MAX_STATE, fd))
        # Validate current independently while the pending file still blocks normal access.
        exact(current, 'version site_id generation updated_at ca_certificate encrypted_key entries signature')
        ca = ca_validate(current, expected, now); entries_validate(current, ca)
        require(pending['encrypted_key'] == current['encrypted_key'] and pending['site_id'] == current['site_id'])
        require(pending['generation'] == current['generation'] + 1 and pending['updated_at'] >= current['updated_at'])
        require(len(current['entries']) <= len(pending['entries']) <= len(current['entries']) + 1)
        changes = 0
        for old, new in zip(current['entries'], pending['entries']):
            require({k: v for k, v in old.items() if k != 'revoked_at'} ==
                    {k: v for k, v in new.items() if k != 'revoked_at'})
            require(old['revoked_at'] is None or new['revoked_at'] == old['revoked_at'])
            changes += old['revoked_at'] != new['revoked_at']
        require(changes <= 1 if len(current['entries']) == len(pending['entries']) else changes == 0)
        # A previous fsync failure is not ignored, even if all bytes are readable now.
        sync_fd = os.open(PENDING, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            private_stat(sync_fd); os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        os.replace(PENDING, STATE, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)


def qualified_runtime():
    require(sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12))
    require(all(version(name) == expected for name, expected in
                [('cryptography','50.0.2'), ('cffi','2.1.1'), ('pycparser','3.11')]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ['init', 'issue', 'revoke', 'export', 'recover']:
        command = commands.add_parser(operation)
        command.add_argument('--directory', required=True, type=Path)
        if operation != 'init':
            command.add_argument('--issuer-sha256', required=True)
        if operation != 'recover':
            command.add_argument('--password-fd', type=int)
        if operation == 'init':
            command.add_argument('--site', required=True)
        if operation == 'issue':
            command.add_argument('--csr', required=True, type=Path)
            command.add_argument('--catalog', required=True, type=Path)
            command.add_argument('--camera', required=True)
            command.add_argument('--role', required=True, choices=['camera', 'ingest'])
            command.add_argument('--ip')
            command.add_argument('--days', type=int, default=7)
        if operation == 'revoke':
            command.add_argument('--serial', required=True)
        if operation == 'export':
            command.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        qualified_runtime()
        password = read_password(args.password_fd) if args.operation != 'recover' else None
        if args.operation == 'init' and args.password_fd is None:
            require(password == prompt_password('Confirm offline issuer passphrase: '))
        if args.operation == 'init':
            print('Issuer SHA256: ' + initialize(args.directory, args.site, password))
        elif args.operation == 'issue':
            serial = issue(args.directory, args.issuer_sha256, password, read_file(absolute(args.csr), 16384),
                           parse(read_file(absolute(args.catalog), 65536)), args.camera, args.role, args.ip, args.days)
            print('Committed certificate serial: ' + serial)
        elif args.operation == 'revoke':
            revoke(args.directory, args.issuer_sha256, password, args.serial)
            print('Revocation committed; export and explicitly apply a fresh bundle.')
        elif args.operation == 'export':
            export(args.directory, args.issuer_sha256, password, args.output)
            print('Export complete; consumer deployment and verification are required.')
        else:
            recover(args.directory, args.issuer_sha256)
            print('Signed pending ledger committed; inspect and export a fresh bundle.')
    except Exception:
        # Input, filesystem and crypto exceptions may contain sensitive material.
        parser.exit(1, 'Enrollment operation failed. Check private inputs, issuer pin, clock and pending state.\n')


if __name__ == '__main__':
    main()
