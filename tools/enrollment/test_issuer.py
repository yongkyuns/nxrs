"""Real certificate, persistence, file-boundary and host mutual-TLS qualification."""
import base64
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

spec = importlib.util.spec_from_file_location('nxrs_issuer', Path(__file__).with_name('issuer.py'))
issuer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issuer)


class IssuerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nxrs-enrollment-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root/'issuer'
        self.password = b'synthetic-test-passphrase-not-a-real-credential-0000'
        self.now = issuer.instant()
        self.pin = issuer.initialize(self.directory, 'site_a', self.password, self.now)
        self.catalog = {'cameras': [dict(camera_id='cam_a', gateway_id='gw_a', site_id='site_a'),
                                    dict(camera_id='cam_b', gateway_id='gw_a', site_id='site_a')]}

    def csr(self, key=None, algorithm=None, malicious=False):
        key = key if key is not None else ec.generate_private_key(ec.SECP256R1())
        builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, 'untrusted requested identity')]))
        if malicious:
            builder = (builder.add_extension(x509.BasicConstraints(True, 9), True)
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName('attacker.invalid')]), False)
                       .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE]), False))
        return key, builder.sign(key, algorithm or hashes.SHA256()).public_bytes(serialization.Encoding.PEM)

    def issue(self, role='camera', camera='cam_a', key=None, malicious=False, **kwargs):
        key, csr = self.csr(key=key, malicious=malicious)
        serial = issuer.issue(self.directory, self.pin, self.password, csr, self.catalog, camera, role,
                              ip='127.0.0.1' if role == 'camera' else None, now=self.now, **kwargs)
        return key, serial

    def state(self):
        return issuer.parse((self.directory/issuer.STATE).read_bytes())

    def bundle(self, name='bundle'):
        output = self.root/name
        issuer.export(self.directory, self.pin, self.password, output, self.now)
        return output, issuer.verify_bundle((output/'bundle.json').read_bytes(), self.pin, 1, now=self.now)

    def test_encrypted_custody_and_public_only_export(self):
        key, serial = self.issue()
        state = self.state()
        self.assertTrue(state['encrypted_key'].startswith('-----BEGIN ENCRYPTED PRIVATE KEY-----'))
        with self.assertRaises(ValueError):
            serialization.load_pem_private_key(state['encrypted_key'].encode(), b'wrong-password')
        output, bundle = self.bundle()
        for path in output.iterdir():
            self.assertNotIn(b'PRIVATE KEY', path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('encrypted_key', bundle)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory/issuer.STATE).stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(issuer.spki(key.public_key()), issuer.spki(issuer.certificate(bundle['entries'][0]['certificate']).public_key()))
        self.assertEqual(bundle['entries'][0]['serial'], serial)
        self.assertFalse((output/'.incomplete').exists())

    def test_csr_cannot_choose_identity_ca_privilege_or_usage(self):
        self.issue(malicious=True)
        self.issue(role='ingest', malicious=True)
        _, bundle = self.bundle()
        server, client = [issuer.certificate(e['certificate']) for e in bundle['entries']]
        self.assertEqual(server.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, 'cam_a')
        self.assertEqual(server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value,
                         x509.SubjectAlternativeName([x509.UniformResourceIdentifier('urn:nxrs:camera:site_a:cam_a'),
                                                       x509.IPAddress(issuer.ipaddress.IPv4Address('127.0.0.1'))]))
        self.assertEqual(client.extensions.get_extension_for_class(x509.SubjectAlternativeName).value,
                         x509.SubjectAlternativeName([x509.UniformResourceIdentifier('urn:nxrs:ingest:site_a:gw_a:cam_a')]))
        for cert, role in [(server, ExtendedKeyUsageOID.SERVER_AUTH), (client, ExtendedKeyUsageOID.CLIENT_AUTH)]:
            self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertEqual(list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value), [role])
            usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
            self.assertTrue(usage.digital_signature)
            self.assertFalse(usage.key_cert_sign or usage.crl_sign or usage.key_encipherment)

    def test_key_reuse_across_devices_roles_renewals_and_revocation_is_rejected(self):
        key, serial = self.issue()
        for role, camera in [('camera', 'cam_a'), ('camera', 'cam_b'), ('ingest', 'cam_a')]:
            with self.assertRaises(ValueError):
                self.issue(role, camera, key=key)
        issuer.revoke(self.directory, self.pin, self.password, serial, self.now)
        with self.assertRaises(ValueError):
            self.issue(key=key)
        self.issue()  # Rotation requires a fresh endpoint key.
        self.assertEqual(len(self.state()['entries']), 2)

    def test_catalog_scope_and_address_errors_do_not_mutate_issuer(self):
        _, csr = self.csr()
        before = (self.directory/issuer.STATE).read_bytes()
        cases = [(self.catalog, 'unknown', 'camera', '127.0.0.1'),
                 ({'cameras':[dict(camera_id='cam_a', gateway_id='gw_a', site_id='other')]}, 'cam_a', 'camera', '127.0.0.1'),
                 ({'cameras':self.catalog['cameras']*2}, 'cam_a', 'camera', '127.0.0.1'),
                 (self.catalog, 'cam_a', 'camera', '8.8.8.8'), (self.catalog, 'cam_a', 'camera', None),
                 (self.catalog, 'cam_a', 'camera', '169.254.1.1'), (self.catalog, 'cam_a', 'camera', '::1'),
                 (self.catalog, 'cam_a', 'ingest', '127.0.0.1')]
        for catalog, camera, role, address in cases:
            with self.subTest(role=role, address=address), self.assertRaises((ValueError, TypeError)):
                issuer.issue(self.directory, self.pin, self.password, csr, catalog, camera, role, address, now=self.now)
            self.assertEqual((self.directory/issuer.STATE).read_bytes(), before)

    def test_invalid_signatures_algorithms_and_multiple_pem_blocks_are_rejected(self):
        bad = []
        for key in [ec.generate_private_key(ec.SECP384R1()), rsa.generate_private_key(public_exponent=65537, key_size=2048)]:
            bad.append(self.csr(key)[1])
        bad.append(self.csr(algorithm=hashes.SHA384())[1])
        _, valid = self.csr()
        csr = x509.load_pem_x509_csr(valid)
        der = csr.public_bytes(serialization.Encoding.DER)
        bad.append(b'-----BEGIN CERTIFICATE REQUEST-----\n' + base64.encodebytes(der[:-1] + bytes([der[-1] ^ 1])) + b'-----END CERTIFICATE REQUEST-----\n')
        bad.extend([valid+valid, b'invalid\n'+valid, valid+b'PRIVATE INPUT MUST NOT BE COPIED', b''])
        for value in bad:
            with self.subTest(length=len(value)), self.assertRaises(Exception):
                issuer.issue(self.directory, self.pin, self.password, value, self.catalog, 'cam_a', 'camera', '127.0.0.1', now=self.now)
        self.assertEqual(self.state()['entries'], [])

    def test_wrong_issuer_passphrase_expiry_and_clock_regression_fail(self):
        _, csr = self.csr()
        for pin, password, when, days in [('0'*64, self.password, self.now, 7),
                (self.pin, b'x'*64, self.now, 7), (self.pin, self.password, self.now-timedelta(seconds=1), 7),
                (self.pin, self.password, self.now+timedelta(days=731), 7),
                (self.pin, self.password, self.now, 0), (self.pin, self.password, self.now, 31)]:
            with self.subTest(days=days), self.assertRaises(Exception):
                issuer.issue(self.directory, pin, password, csr, self.catalog, 'cam_a', 'camera', '127.0.0.1', days, when)
        self.assertFalse((self.directory/issuer.PENDING).exists())

    def test_revocation_is_durable_idempotent_and_does_not_select_other_device(self):
        _, a = self.issue()
        _, b = self.issue(camera='cam_b')
        issuer.revoke(self.directory, self.pin, self.password, a, self.now)
        generation = self.state()['generation']
        issuer.revoke(self.directory, self.pin, self.password, a, self.now)
        self.assertEqual(self.state()['generation'], generation)
        with self.assertRaises(ValueError):
            issuer.revoke(self.directory, self.pin, self.password, 'unknown', self.now)
        _, data = self.bundle()
        self.assertEqual({e['serial']: e['revoked_at'] is not None for e in data['entries']}, {a:True, b:False})
        crl = x509.load_pem_x509_crl(data['revocations'].encode())
        self.assertEqual([e.serial_number for e in crl], [int(a,16)])

    def test_bundle_signature_expiration_and_generation_high_water(self):
        self.issue()
        output, data = self.bundle()
        payload = (output/'bundle.json').read_bytes()
        issuer.verify_bundle(payload, self.pin, data['generation'], self.now+timedelta(hours=23))
        for minimum, when in [(data['generation']+1,self.now), (1,self.now+timedelta(hours=24)),
                              (1,self.now-timedelta(minutes=6))]:
            with self.assertRaises(ValueError):
                issuer.verify_bundle(payload, self.pin, minimum, when)
        altered = deepcopy(data); altered['entries'][0]['camera_id'] = 'cam_b'
        with self.assertRaises(Exception):
            issuer.verify_bundle(issuer.canonical(altered), self.pin, 1, now=self.now)
        with self.assertRaises(ValueError):
            issuer.parse(b'{"version":1,"version":2}')

    def test_ledger_tampering_missing_state_and_unsafe_files_are_rejected(self):
        state_path = self.directory/issuer.STATE
        original = state_path.read_bytes()
        data = self.state(); data['generation'] += 1
        state_path.write_bytes(issuer.canonical(data))
        with self.assertRaises(Exception): self.bundle()
        state_path.write_bytes(original)
        state_path.chmod(0o644)
        with self.assertRaises(ValueError): self.bundle()
        state_path.chmod(0o600)
        other = self.directory/'other'; state_path.rename(other)
        state_path.symlink_to(other)
        with self.assertRaises(OSError): self.bundle()
        state_path.unlink(); os.link(other, state_path)
        with self.assertRaises(ValueError): self.bundle()
        state_path.unlink(); other.rename(state_path)
        state_path.unlink()
        with self.assertRaises(FileNotFoundError): self.bundle()

    def test_exclusive_owner_and_unsafe_lock(self):
        with issuer.locked(self.directory):
            with self.assertRaises(BlockingIOError): self.bundle()
        lock = self.directory/'.issuer.lock'
        lock.chmod(0o644)
        with self.assertRaises(ValueError): self.bundle()
        lock.chmod(0o600); lock.unlink()
        target = self.root/'empty'; target.write_bytes(b''); target.chmod(0o600)
        lock.symlink_to(target)
        with self.assertRaises(OSError): self.bundle()

    def test_signed_pending_issue_recovers_without_reissuing(self):
        with mock.patch.object(issuer.os, 'replace', side_effect=OSError('injected replacement failure')):
            with self.assertRaises(OSError): self.issue()
        self.assertTrue((self.directory/issuer.PENDING).exists())
        with self.assertRaises(ValueError): self.bundle()
        with self.assertRaises(ValueError): self.issue()
        issuer.recover(self.directory, self.pin, self.now)
        self.assertEqual(len(self.state()['entries']),1)
        self.bundle()

    def test_signed_pending_revocation_recovers_without_resurrecting_denial(self):
        _, serial = self.issue()
        with mock.patch.object(issuer.os, 'replace', side_effect=OSError('injected failure')):
            with self.assertRaises(OSError): issuer.revoke(self.directory, self.pin, self.password, serial, self.now)
        with self.assertRaises(ValueError): self.bundle()
        issuer.recover(self.directory, self.pin, self.now)
        self.assertIsNotNone(self.state()['entries'][0]['revoked_at'])
        self.bundle()

    def test_incomplete_pending_file_blocks_all_automatic_recovery(self):
        before = (self.directory/issuer.STATE).read_bytes()
        with mock.patch.object(issuer.os, 'write', side_effect=OSError('injected write failure')):
            with self.assertRaises(OSError): self.issue()
        self.assertEqual((self.directory/issuer.STATE).read_bytes(), before)
        self.assertTrue((self.directory/issuer.PENDING).exists())
        with self.assertRaises(ValueError): self.bundle()
        with self.assertRaises(Exception): issuer.recover(self.directory, self.pin, self.now)
        self.assertTrue((self.directory/issuer.PENDING).exists())

    def test_failed_final_sync_has_no_credential_reuse_or_false_success(self):
        key, csr = self.csr()
        with mock.patch.object(issuer.os, 'fsync', side_effect=[None, None, OSError('injected final sync failure')]):
            with self.assertRaises(OSError):
                issuer.issue(self.directory,self.pin,self.password,csr,self.catalog,'cam_a','camera','127.0.0.1',now=self.now)
        self.assertEqual(len(self.state()['entries']),1)
        with self.assertRaises(ValueError): self.issue(key=key)
        self.bundle()

    def test_incomplete_export_is_marked_and_never_overwritten(self):
        self.issue()
        real = issuer.new_file
        def fail(fd, name, data):
            if name == 'bundle.json': raise OSError('injected output failure')
            return real(fd, name, data)
        with mock.patch.object(issuer, 'new_file', side_effect=fail):
            with self.assertRaises(OSError): self.bundle()
        self.assertTrue((self.root/'bundle/.incomplete').exists())
        with self.assertRaises(FileExistsError): self.bundle()
        self.bundle('complete')

    def test_capacity_and_random_serial_collision_fail_before_persistence(self):
        _, serial = self.issue()
        with mock.patch.object(issuer, 'MAX_ENTRIES',1):
            with self.assertRaises(ValueError): self.issue()
        with mock.patch.object(issuer.x509, 'random_serial_number',return_value=int(serial,16)):
            with self.assertRaises(ValueError): self.issue()
        self.assertEqual(len(self.state()['entries']),1)
        self.assertFalse((self.directory/issuer.PENDING).exists())

    def test_cli_fd_privacy_and_fifo_rejection_have_a_process_deadline(self):
        password_path = self.root/'password'; password_path.write_bytes(self.password); password_path.chmod(0o600)
        fifo = self.root/'synthetic-secret-marker'; os.mkfifo(fifo,0o600)
        catalog = self.root/'catalog'; catalog.write_text(json.dumps(self.catalog)); catalog.chmod(0o600)
        def command(args):
            with password_path.open('rb') as stream:
                return subprocess.run([sys.executable,str(Path(issuer.__file__))]+args+
                    ['--password-fd',str(stream.fileno())],pass_fds=[stream.fileno()],capture_output=True,timeout=5)
        result = command(['issue','--directory',str(self.directory),'--issuer-sha256',self.pin,
                          '--csr',str(fifo),'--catalog',str(catalog),'--camera','cam_a','--role','camera','--ip','127.0.0.1'])
        self.assertEqual(result.returncode,1)
        self.assertNotIn(b'synthetic-secret-marker',result.stdout+result.stderr)
        self.assertNotIn(self.password,result.stdout+result.stderr)
        (self.directory/'.issuer.lock').unlink(); os.mkfifo(self.directory/'.issuer.lock',0o600)
        result = command(['export','--directory',str(self.directory),'--issuer-sha256',self.pin,'--output',str(self.root/'out')])
        self.assertEqual(result.returncode,1)
        password_path.chmod(0o644)
        result = command(['export','--directory',str(self.directory),'--issuer-sha256',self.pin,'--output',str(self.root/'out')])
        self.assertEqual(result.returncode,1)

    def test_initialization_never_overwrites_or_accepts_weak_passphrases(self):
        with self.assertRaises(FileExistsError): issuer.initialize(self.directory,'site_a',self.password,self.now)
        with self.assertRaises(ValueError): issuer.initialize(self.root/'new','site_a',b'short',self.now)
        self.assertFalse((self.root/'new').exists())
        with self.assertRaises(ValueError): issuer.initialize(self.root/'new','../site',self.password,self.now)

    def tls_exchange(self, output, server_key, server_serial, client_key, client_serial, hostname='127.0.0.1', client_trust=None):
        def key_path(key, name):
            path = self.root/name
            path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            path.chmod(0o600)
            return path
        trust = self.root/'tls-trust.pem'
        trust.write_bytes((output/'ca.pem').read_bytes()+(output/'revocations.pem').read_bytes())
        trust.chmod(0o600)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.minimum_version = ssl.TLSVersion.TLSv1_2
        server.load_cert_chain(output/f'cert-{server_serial}.pem',key_path(server_key,'server-test-key.pem'))
        server.load_verify_locations(cafile=trust)
        server.verify_mode = ssl.CERT_REQUIRED
        server.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_X509_STRICT
        client = ssl.create_default_context(cafile=client_trust or trust)
        client.minimum_version = ssl.TLSVersion.TLSv1_2
        client.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_X509_STRICT
        if client_key is not None:
            client.load_cert_chain(output/f'cert-{client_serial}.pem',key_path(client_key,'client-test-key.pem'))
        errors = []
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen(1); listener.settimeout(3)
            def serve():
                try:
                    connection,_ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        with server.wrap_socket(connection,server_side=True) as tls:
                            self.assertEqual(tls.recv(1),b'?')
                            tls.sendall(b'!')
                except Exception as error: errors.append(error)
            worker = threading.Thread(target=serve,daemon=True); worker.start()
            try:
                with socket.create_connection(listener.getsockname(),timeout=3) as connection:
                    with client.wrap_socket(connection,server_hostname=hostname) as tls:
                        tls.sendall(b'?')
                        self.assertEqual(tls.recv(1),b'!')
            finally:
                worker.join(4)
                self.assertFalse(worker.is_alive(), 'TLS witness did not quiesce')
        if errors: raise errors[0]

    def test_real_mutual_tls_and_negative_role_hostname_missing_and_revoked_client(self):
        server_key, server = self.issue()
        client_key, client = self.issue('ingest')
        output,_ = self.bundle()
        self.tls_exchange(output,server_key,server,client_key,client)
        other = self.root/'other-issuer'
        other_pin = issuer.initialize(other,'site_a',self.password,self.now)
        other_output = self.root/'other-bundle'
        issuer.export(other,other_pin,self.password,other_output,self.now)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.tls_exchange(output,server_key,server,client_key,client,client_trust=other_output/'ca.pem')
        for key, serial, host in [(None,None,'127.0.0.1'),(server_key,server,'127.0.0.1'),(client_key,client,'127.0.0.2')]:
            with self.subTest(client=serial,host=host), self.assertRaises(ssl.SSLError):
                self.tls_exchange(output,server_key,server,key,serial,host)
        issuer.revoke(self.directory,self.pin,self.password,client,self.now)
        output,_ = self.bundle('revoked')
        with self.assertRaises(ssl.SSLError): self.tls_exchange(output,server_key,server,client_key,client)

    def test_signed_pending_cannot_clear_a_prior_denial(self):
        _, serial = self.issue()
        issuer.revoke(self.directory,self.pin,self.password,serial,self.now)
        data = self.state()
        ca = issuer.certificate(data['ca_certificate'])
        key = issuer.issuer_key(data,ca,self.password)
        data['entries'][0]['revoked_at'] = None
        data['generation'] += 1
        payload = issuer.canonical(issuer.signed({k:v for k,v in data.items() if k!='signature'},key))
        path = self.directory/issuer.PENDING
        path.write_bytes(payload); path.chmod(0o600)
        with self.assertRaises(ValueError): issuer.recover(self.directory,self.pin,self.now)
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.state()['entries'][0]['revoked_at'])

    def test_export_refuses_clock_regression(self):
        with self.assertRaises(ValueError):
            issuer.export(self.directory,self.pin,self.password,self.root/'out',self.now-timedelta(seconds=1))
        self.assertFalse((self.root/'out').exists())

    def test_real_tls_rejects_expired_server_and_client_with_fresh_revocation_data(self):
        actual_now = self.now
        for expired_role in ['camera','ingest']:
            with self.subTest(expired_role=expired_role):
                self.directory = self.root/('expired-'+expired_role+'-issuer')
                self.pin = issuer.initialize(self.directory,'site_a',self.password,actual_now-timedelta(days=3))
                self.now = actual_now-timedelta(days=2)
                old_key, old_serial = self.issue(expired_role,days=1)
                self.now = actual_now
                other_role = 'ingest' if expired_role=='camera' else 'camera'
                valid_key, valid_serial = self.issue(other_role)
                output,_ = self.bundle('expired-'+expired_role+'-bundle')
                keys = {expired_role:(old_key,old_serial), other_role:(valid_key,valid_serial)}
                with self.assertRaises(ssl.SSLError):
                    self.tls_exchange(output,*keys['camera'],*keys['ingest'])
                # A fresh replacement succeeds under the same CA, clock and mapping.
                keys[expired_role] = self.issue(expired_role)
                output,_ = self.bundle('renewed-'+expired_role+'-bundle')
                self.tls_exchange(output,*keys['camera'],*keys['ingest'])

    def test_signed_bundle_rejects_wrong_crl_signature_and_wrong_revocation_contents(self):
        _, serial = self.issue()
        issuer.revoke(self.directory,self.pin,self.password,serial,self.now)
        _, data = self.bundle()
        state = self.state(); ca = issuer.certificate(state['ca_certificate'])
        key = issuer.issuer_key(state,ca,self.password)
        for crl_key in [key, ec.generate_private_key(ec.SECP256R1())]:
            empty = (x509.CertificateRevocationListBuilder().issuer_name(ca.subject)
                     .last_update(self.now).next_update(self.now+timedelta(hours=24))
                     .add_extension(x509.CRLNumber(data['generation']),False)
                     .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),False)
                     .sign(crl_key,hashes.SHA256()))
            bad = deepcopy(data)
            bad['revocations'] = empty.public_bytes(serialization.Encoding.PEM).decode()
            payload = issuer.canonical(issuer.signed({k:v for k,v in bad.items() if k!='signature'},key))
            with self.assertRaises(ValueError):
                issuer.verify_bundle(payload,self.pin,1,self.now)

    def test_ca_key_cannot_be_enrolled_as_an_endpoint(self):
        state = self.state(); ca = issuer.certificate(state['ca_certificate'])
        key = issuer.issuer_key(state,ca,self.password)
        with self.assertRaises(ValueError): self.issue(key=key)
        self.assertEqual(self.state()['entries'],[])

    def test_interactive_password_never_falls_back_to_echoed_input(self):
        def unusable_terminal(_):
            warnings.warn('terminal cannot disable echo',issuer.getpass.GetPassWarning)
            self.fail('unusable terminal continued after warning')
        with mock.patch.object(issuer.sys.stdin,'isatty',return_value=True), mock.patch.object(issuer.getpass,'getpass',side_effect=unusable_terminal):
            with self.assertRaises(issuer.getpass.GetPassWarning): issuer.read_password(None)

    def test_real_cli_initialization_issue_export_revoke_and_recovery(self):
        password = self.root/'cli-passphrase'
        password.write_bytes(self.password); password.chmod(0o600)
        def run(operation, directory, args, use_password=True):
            command = [sys.executable,issuer.__file__,operation,'--directory',str(directory)]+args
            with password.open('rb') as stream:
                if use_password: command += ['--password-fd',str(stream.fileno())]
                result = subprocess.run(command,pass_fds=[stream.fileno()],capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertNotIn(self.password,result.stdout+result.stderr)
            return result.stdout.decode().strip()
        directory = self.root/'cli-issuer'
        text = run('init',directory,['--site','site_a'])
        pin = text.removeprefix('Issuer SHA256: ')
        self.assertRegex(pin,r'^[0-9a-f]{64}$')
        _, csr = self.csr()
        request = self.root/'cli.csr'; request.write_bytes(csr); request.chmod(0o600)
        catalog = self.root/'cli-catalog'; catalog.write_text(json.dumps(self.catalog)); catalog.chmod(0o600)
        common = ['--issuer-sha256',pin]
        text = run('issue',directory,common+['--csr',str(request),'--catalog',str(catalog),
                    '--camera','cam_a','--role','camera','--ip','127.0.0.1'])
        serial = text.removeprefix('Committed certificate serial: ')
        output = self.root/'cli-export'
        run('export',directory,common+['--output',str(output)])
        bundle = issuer.verify_bundle((output/'bundle.json').read_bytes(),pin,2)
        self.assertEqual(bundle['entries'][0]['serial'],serial)
        run('revoke',directory,common+['--serial',serial])
        output = self.root/'cli-revoked'
        run('export',directory,common+['--output',str(output)])
        bundle = issuer.verify_bundle((output/'bundle.json').read_bytes(),pin,3)
        self.assertIsNotNone(bundle['entries'][0]['revoked_at'])
        # Recover a separately injected but fully signed pending operation through the CLI.
        with mock.patch.object(issuer.os,'replace',side_effect=OSError('injected')):
            with self.assertRaises(OSError): self.issue()
        run('recover',self.directory,['--issuer-sha256',self.pin],use_password=False)
        self.assertFalse((self.directory/issuer.PENDING).exists())

    def test_refresh_reserves_distinct_crl_numbers_and_binds_the_issuer_key(self):
        self.issue()
        output, first = self.bundle()
        self.now += timedelta(hours=1)
        _, second = self.bundle('refreshed')
        self.assertGreater(second['generation'],first['generation'])
        self.assertEqual(first['entries'],second['entries'])
        crl = x509.load_pem_x509_crl(second['revocations'].encode())
        ca = issuer.certificate(second['ca_certificate'])
        self.assertEqual(crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number,second['generation'])
        self.assertEqual(crl.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier,
                         ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest)
        self.assertEqual(self.state()['generation'],second['generation'])

    def test_failed_crl_sequence_commit_cannot_publish_and_recovery_preserves_denials(self):
        _, serial = self.issue()
        issuer.revoke(self.directory,self.pin,self.password,serial,self.now)
        old = self.state()
        with mock.patch.object(issuer.os,'replace',side_effect=OSError('injected sequence failure')):
            with self.assertRaises(OSError): self.bundle()
        self.assertFalse((self.root/'bundle').exists())
        with self.assertRaises(ValueError): self.bundle()
        issuer.recover(self.directory,self.pin,self.now)
        self.assertEqual(self.state()['entries'],old['entries'])
        _, bundle = self.bundle()
        self.assertGreater(bundle['generation'],old['generation']+1)
        self.assertIsNotNone(bundle['entries'][0]['revoked_at'])


if __name__ == '__main__':
    unittest.main()
