#!/usr/bin/env python3
"""Actual Rust TLS ingest process boundary with synthetic enrollment and test encoder.

This tests the gateway, not NuttX TLS or a physical sensor. No generated keys or
raw remote/process output are printed or retained as artifacts.
"""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools/enrollment'))
import issuer
import prepare_ingest

BINARY = None
RESPONSE = b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\n\r\n'
# Envelope witness only, not a decodable JPEG; the real-media integration remains separate.
JPEG = bytes([255,216,255,192,0,11,8,0,240,1,64,1,1,17,0,255,218,0,2,255,217])


def write(path,data):
    with open(path,'xb',opener=lambda name,flags:os.open(name,flags,0o600)) as output:
        output.write(data if isinstance(data,bytes) else data.encode())


class TlsIngestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nxrs-tls-ingest-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.password = b'synthetic-issuer-passphrase-not-production-000000'
        self.now = issuer.instant()
        self.pin = issuer.initialize(self.root/'issuer','site',self.password,self.now)
        self.catalog = {'cameras':[dict(camera_id='cam',gateway_id='gw',site_id='site',stream_path='reference'),
                                    dict(camera_id='other',gateway_id='gw',site_id='site',stream_path='other')],
                        'publishers':[dict(username='publisher',camera_id='cam')]}
        write(self.root/'catalog.json',json.dumps(self.catalog))
        write(self.root/'publisher.token','1'*64+'\n')
        self.marker = self.root/'encoder-started'
        self.encoder = self.root/'encoder'
        write(self.encoder, f'#!{sys.executable}\nimport os,sys,time\n'
            f'f=os.open({str(self.marker)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n'
            'os.write(f,str(os.getpid()).encode());os.close(f)\n'
            'count=0\nwhile sys.stdin.buffer.read(1):\n count+=1;sys.stdout.buffer.write(b"x");sys.stdout.flush();sys.stderr.write("frame="+str(count)+"\\n");sys.stderr.flush()\n')
        self.encoder.chmod(0o700)
        self.publisher_marker = self.root/'publisher-started'
        self.publisher = self.root/'publisher'
        write(self.publisher, f'#!{sys.executable}\nimport os,sys\n'
            f'f=os.open({str(self.publisher_marker)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n'
            'os.write(f,str(os.getpid()).encode());os.close(f)\n'
            'token=sys.stdin.buffer.read(64)\nassert len(token)==64\n'
            'count=0\nwhile sys.stdin.buffer.read(1):\n count+=1;sys.stdout.write("packet="+str(count)+"\\n");sys.stdout.flush()\n')
        self.publisher.chmod(0o700)
        self.keys = {}
        self.server_serial = self.enroll('camera')
        self.client_serial = self.enroll('ingest')
        self.bundle = self.root/'bundle'
        issuer.export(self.root/'issuer',self.pin,self.password,self.bundle,self.now)
        self.server = None; self.process = None; self.handlers = set()
        self.requests = 0; self.connections = 0; self.active = 0; self.peak = 0
        self.silent = False
        self.raw_handshake = False
        self.malformed_tls = False
        self.drop_handshake = False
        self.loop_errors = []
        def transport_error(loop, context):
            error = context.get('exception')
            if not isinstance(error,(ssl.SSLError,ConnectionResetError,ConnectionAbortedError)):
                self.loop_errors.append(type(error).__name__)
        asyncio.get_running_loop().set_exception_handler(transport_error)
        self.ssl_context = self.context()
        self.server = await asyncio.start_server(self.accept,'127.0.0.1',0,ssl=self.ssl_context,ssl_handshake_timeout=1)
        self.port = self.server.sockets[0].getsockname()[1]
        self.config = dict(camera=f'127.0.0.1:{self.port}',allow_insecure_camera_http=True,rtsp='127.0.0.1:8554',
            stream_path='reference',publisher_user='publisher',publisher_token_file=str(self.root/'publisher.token'),
            ffmpeg=str(self.encoder),publisher=str(self.publisher),width=320,height=240,frame_timeout_ms=500)
        self.baseline = self.root/'base.json'; write(self.baseline,json.dumps(self.config))
        self.stage = self.root/'stage'
        self.config_path = self.prepare(self.stage)

    def enroll(self,role,camera='cam',when=None,days=7,directory=None):
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key,hashes.SHA256())
               .public_bytes(serialization.Encoding.PEM))
        serial = issuer.issue(directory or self.root/'issuer',self.pin,self.password,csr,self.catalog,camera,role,
            '127.0.0.1' if role=='camera' else None,days,when or self.now)
        path = self.root/(serial+'.key')
        write(path,key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        self.keys[serial] = path
        return serial

    def context(self,serial=None,tls12=False):
        serial = serial or self.server_serial
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if tls12: ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.bundle/('cert-'+serial+'.pem'),self.keys[serial])
        trust = self.root/('trust-'+str(len(list(self.root.glob('trust-*'))))+'.pem')
        write(trust,(self.bundle/'ca.pem').read_bytes()+(self.bundle/'revocations.pem').read_bytes())
        ctx.load_verify_locations(cafile=trust); ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_X509_STRICT
        return ctx

    def prepare(self,output,camera_serial=None,client_serial=None):
        return prepare_ingest.prepare(output,self.bundle/'bundle.json',self.pin,1,self.root/'catalog.json',self.baseline,
            'cam',camera_serial or self.server_serial,client_serial or self.client_serial,
            self.keys[client_serial or self.client_serial])

    async def asyncTearDown(self):
        await self.stop()
        if self.server:
            self.server.close(); await self.server.wait_closed()
        for task in list(self.handlers): task.cancel()
        await asyncio.gather(*list(self.handlers),return_exceptions=True)
        self.handlers.clear()
        self.assertEqual(self.loop_errors,[])

    async def accept(self,reader,writer):
        task = asyncio.current_task(); self.handlers.add(task)
        self.connections+=1; self.active+=1; self.peak=max(self.peak,self.active)
        try:
            if self.raw_handshake:
                if self.malformed_tls:
                    writer.write(b'not a TLS record'); await writer.drain(); return
                if self.drop_handshake:
                    await reader.read(1024); return
                while await reader.read(8192): pass
                return
            peer = writer.get_extra_info('peercert')
            if ('URI','urn:nxrs:ingest:site:gw:cam') not in peer.get('subjectAltName',[]): return
            if self.silent:
                await asyncio.sleep(20); return
            request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),2)
            if not request.startswith(b'GET /stream HTTP/1.1\r\n'): return
            self.requests+=1
            writer.write(RESPONSE); await writer.drain()
            for sequence in range(1,200):
                writer.write(f'--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(JPEG)}\r\nX-Sequence: {sequence}\r\nX-Capture-Ms: {sequence*50}\r\n\r\n'.encode()+JPEG+b'\r\n')
                await writer.drain(); await asyncio.sleep(.05)
        except (ConnectionError,ssl.SSLError,asyncio.TimeoutError,asyncio.IncompleteReadError): pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError,ssl.SSLError): pass
            self.handlers.discard(task); self.active-=1

    async def start(self,path=None,stderr=asyncio.subprocess.DEVNULL):
        self.process = await asyncio.create_subprocess_exec(str(BINARY),str(path or self.config_path),'--require-camera-tls',
            stdout=asyncio.subprocess.DEVNULL,stderr=stderr)

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(),8)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait(); self.fail('owned ingest failed to retire')
        self.process = None

    async def until(self,test,seconds=3):
        deadline = asyncio.get_running_loop().time()+seconds
        while not test():
            if asyncio.get_running_loop().time()>=deadline: self.fail('bounded gateway witness did not complete')
            await asyncio.sleep(.02)

    async def rejected(self,config=None,check=False,path=None):
        if config is not None:
            path=self.root/'rejected.json'; write(path,json.dumps(config))
        else: path=path or self.config_path
        args=[str(BINARY),str(path),'--require-camera-tls']+(['--check-config'] if check else [])
        process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await asyncio.wait_for(process.communicate(),4)
        self.assertNotEqual(process.returncode,0)
        self.assertNotIn(self.password,stdout+stderr)
        self.assertFalse(self.marker.exists());self.assertFalse(self.publisher_marker.exists())
        self.assertEqual(self.requests,0)

    async def test_valid_mutual_tls_scope_single_source_and_cancel_reaps_encoder(self):
        subprocess.run([str(BINARY),str(self.config_path),'--require-camera-tls','--check-config'],check=True,timeout=5)
        await self.start(); await self.until(lambda:self.marker.exists() and self.marker.stat().st_size>0)
        self.assertEqual(self.connections,1); self.assertEqual(self.peak,1)
        await self.until(lambda:self.publisher_marker.exists() and self.publisher_marker.stat().st_size>0)
        pids=[int(self.marker.read_text()),int(self.publisher_marker.read_text())]
        await self.stop()
        await self.until(lambda:self.active==0)
        for pid in pids:
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)

    async def test_encoder_can_produce_its_first_output_after_the_steady_frame_budget(self):
        # Consume/produce after 1.2s: beyond this fixture's 0.5s steady budget,
        # within the existing 10s encoder/publication startup policy.
        encoded = self.encoder.read_text().replace('count=0\n', 'time.sleep(1.2)\ncount=0\n', 1)
        self.encoder.write_text(encoded)
        observed = self.root/'publisher-received-encoded-byte'
        source = self.publisher.read_text()
        before = ' count+=1;sys.stdout.write'
        after = ' if count==0: open('+repr(str(observed))+',"x").write("1")\n count+=1;sys.stdout.write'
        self.assertIn(before, source); self.publisher.write_text(source.replace(before,after,1))
        await self.start()
        await self.until(lambda:all(p.exists() and p.stat().st_size>0 for p in [self.marker,self.publisher_marker]))
        pids=[int(self.marker.read_text()),int(self.publisher_marker.read_text())]
        await self.until(observed.exists,seconds=3)
        self.assertEqual(self.connections,1);self.assertEqual(self.peak,1)
        self.assertIsNone(self.process.returncode)
        await self.stop();await self.until(lambda:self.active==0)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    async def test_silent_authenticated_camera_reports_setup_timeout_without_private_text(self):
        self.silent=True
        log=self.root/'observed-stages'
        with log.open('xb') as stream:
            await self.start(stderr=stream)
            await self.until(lambda:b'stage-failure (CameraSetup: Timeout)' in log.read_bytes(),seconds=6)
            await self.stop()
        data=log.read_bytes()
        self.assertNotIn(self.password,data);self.assertNotIn(str(self.root).encode(),data)
        self.assertFalse(self.marker.exists());self.assertFalse(self.publisher_marker.exists())

    async def test_first_encoded_byte_does_not_extend_the_steady_stall_budget(self):
        source = self.encoder.read_text()
        before = 'count=0\nwhile sys.stdin.buffer.read(1):'
        after = 'sys.stdout.buffer.write(b"x");sys.stdout.flush();sys.stderr.write("frame=1\\n");sys.stderr.flush()\ntime.sleep(20)\ncount=0\nwhile sys.stdin.buffer.read(1):'
        self.assertIn(before,source);self.encoder.write_text(source.replace(before,after,1))
        log=self.root/'observed-relay-stage'
        stream=log.open('xb');self.addCleanup(stream.close)
        await self.start(stderr=stream)
        await self.until(lambda:all(p.exists() and p.stat().st_size>0 for p in [self.marker,self.publisher_marker]))
        pids=[int(self.marker.read_text()),int(self.publisher_marker.read_text())]
        def retired():
            for pid in pids:
                try:os.kill(pid,0);return False
                except ProcessLookupError:pass
            return True
        await self.until(retired,seconds=1.2)
        self.assertIn(b'stage-failure (EncodedRelay: Timeout)',log.read_bytes())
        self.assertEqual(self.peak,1)
        await self.stop();await self.until(lambda:self.active==0)

    async def child_failure(self,which):
        await self.start()
        await self.until(lambda:all(p.exists() and p.stat().st_size>0 for p in [self.marker,self.publisher_marker]))
        pids=[int(self.marker.read_text()),int(self.publisher_marker.read_text())]
        os.kill(pids[which],9)
        def gone():
            for pid in pids:
                try: os.kill(pid,0); return False
                except ProcessLookupError: pass
            return True
        await self.until(gone)
        await self.stop()

    async def test_encoder_death_retires_publisher_before_retry(self):
        await self.child_failure(0)

    async def test_publisher_death_retires_encoder_before_retry(self):
        await self.child_failure(1)

    async def test_publisher_spawn_failure_never_starts_encoder(self):
        config=json.loads(self.config_path.read_text()); config['publisher']=str(self.root/'missing-publisher')
        path=self.root/'missing.json';write(path,json.dumps(config))
        await self.start(path);await self.until(lambda:self.requests>0)
        await self.until(lambda:self.active==0)
        await self.stop()
        self.assertFalse(self.marker.exists());self.assertFalse(self.publisher_marker.exists())

    async def test_cleartext_requires_an_explicit_development_launch(self):
        await self.rejected(self.config)

    async def test_wrong_camera_gateway_site_issuer_and_generation_are_rejected_before_network(self):
        original=json.loads(self.config_path.read_text())
        for field,value in [('camera_id','other'),('gateway_id','other'),('site_id','other'),
                            ('issuer_sha256','0'*64),('enrollment_generation',1)]:
            config=json.loads(json.dumps(original)); config['camera_tls'][field]=value
            path=self.root/('bad-'+field+'.json'); write(path,json.dumps(config))
            await self.rejected_at(path)
        self.assertEqual(self.connections,0)

    async def rejected_at(self,path):
        p=await asyncio.create_subprocess_exec(str(BINARY),str(path),'--require-camera-tls','--check-config',
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        self.assertNotEqual(await asyncio.wait_for(p.wait(),4),0)
        self.assertFalse(self.marker.exists()); self.assertEqual(self.requests,0)

    async def test_exact_approved_server_leaf_is_required_even_for_same_camera_and_ca(self):
        replacement=self.enroll('camera')
        updated=self.root/'updated'
        issuer.export(self.root/'issuer',self.pin,self.password,updated,self.now)
        self.bundle=updated
        self.ssl_context=self.context(replacement)
        self.server.close(); await self.server.wait_closed()
        self.server=await asyncio.start_server(self.accept,'127.0.0.1',self.port,ssl=self.ssl_context,ssl_handshake_timeout=1)
        # The original approved leaf remains valid, but its replacement was not approved in this config.
        await self.rejected()
        path=self.prepare(self.root/'approved-rotation',camera_serial=replacement)
        await self.start(path); await self.until(lambda:self.marker.exists() and self.marker.stat().st_size>0)
        await self.stop()

    async def test_wrong_client_key_and_ambiguous_http_acknowledgement_are_rejected(self):
        original=json.loads(self.config_path.read_text())
        original['camera_tls']['client_key_file']=str(self.keys[self.server_serial])
        await self.rejected(original,check=True)
        path=self.root/'bad-ack.json'; original=json.loads(self.config_path.read_text()); original['allow_insecure_camera_http']=True
        write(path,json.dumps(original)); await self.rejected_at(path)

    async def test_malformed_credentials_and_symlinks_fail_before_encoder(self):
        peer=self.stage/'camera-peer.pem'; backup=peer.read_bytes()
        peer.write_bytes(b'invalid PEM contains synthetic-private-marker')
        await self.rejected(check=True)
        peer.write_bytes(backup); peer.chmod(0o644)
        await self.rejected(check=True)
        peer.chmod(0o600); peer.unlink(); peer.symlink_to(self.bundle/('cert-'+self.server_serial+'.pem'))
        await self.rejected(check=True)

    async def test_tls12_is_supported(self):
        self.server.close(); await self.server.wait_closed()
        self.server=await asyncio.start_server(self.accept,'127.0.0.1',self.port,ssl=self.context(tls12=True),ssl_handshake_timeout=1)
        await self.start(); await self.until(lambda:self.marker.exists() and self.marker.stat().st_size>0); await self.stop()
        self.assertEqual(self.connections,1)

    async def test_cancel_during_tls_http_setup_never_spawns_encoder(self):
        self.silent=True
        await self.start(); await self.until(lambda:self.connections==1)
        await self.stop(); self.assertFalse(self.marker.exists())

    async def revoke_and_reject(self,serial,name):
        issuer.revoke(self.root/'issuer',self.pin,self.password,serial,self.now)
        updated=self.root/('revoked-'+name)
        issuer.export(self.root/'issuer',self.pin,self.password,updated,self.now)
        # Bypass frontend refusal to exercise the actual Rust CRL verifier.
        (self.stage/'camera-crl.pem').write_bytes((updated/'revocations.pem').read_bytes())
        config=json.loads(self.config_path.read_text())
        config['camera_tls']['enrollment_generation']=issuer.parse((updated/'bundle.json').read_bytes())['generation']
        path=self.root/('revoked-'+name+'.json'); write(path,json.dumps(config)); await self.rejected_at(path)

    async def test_revoked_camera_is_rejected_by_actual_rust_verifier(self):
        await self.revoke_and_reject(self.server_serial,'server')

    async def test_revoked_client_is_rejected_by_actual_rust_verifier(self):
        await self.revoke_and_reject(self.client_serial,'client')

    async def test_crl_expiration_retires_an_active_source_and_encoder(self):
        # A signed short lease directly tests runtime expiry without changing host time.
        state=issuer.parse((self.root/'issuer'/issuer.STATE).read_bytes()); ca=issuer.certificate(state['ca_certificate'])
        key=issuer.issuer_key(state,ca,self.password)
        crl=(x509.CertificateRevocationListBuilder().issuer_name(ca.subject).last_update(self.now)
             .next_update(self.now+timedelta(seconds=5)).add_extension(x509.CRLNumber(state['generation']),False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),False)
             .sign(key,hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
        (self.stage/'camera-crl.pem').write_bytes(crl)
        config=json.loads(self.config_path.read_text()); config['camera_tls']['enrollment_generation']=state['generation']
        path=self.root/'short-lease.json'; write(path,json.dumps(config))
        await self.start(path); await self.until(lambda:self.marker.exists() and self.marker.stat().st_size>0)
        pid=int(self.marker.read_text())
        self.assertNotEqual(await asyncio.wait_for(self.process.wait(),8),0)
        await self.until(lambda:self.active==0)
        with self.assertRaises(ProcessLookupError): os.kill(pid,0)

    async def plain_peer(self,malformed=False):
        self.server.close(); await self.server.wait_closed()
        self.raw_handshake=True; self.malformed_tls=malformed
        self.server=await asyncio.start_server(self.accept,'127.0.0.1',self.port)

    async def test_cancel_during_incomplete_handshake_closes_socket_without_encoder(self):
        await self.plain_peer()
        await self.start(); await self.until(lambda:self.connections==1)
        await self.stop(); await self.until(lambda:self.active==0)
        self.assertFalse(self.marker.exists())

    async def test_whole_handshake_deadline_retries_without_overlapping_ownership(self):
        await self.plain_peer()
        await self.start(); await self.until(lambda:self.connections>=2,seconds=9)
        self.assertEqual(self.peak,1); self.assertFalse(self.marker.exists())
        await self.stop(); await self.until(lambda:self.active==0)

    async def test_malformed_tls_fails_closed_without_http_or_encoder(self):
        await self.plain_peer(malformed=True)
        await self.rejected()

    async def test_root_rollover_requires_explicit_new_trust_and_retires_old_root(self):
        directory=self.root/'new-issuer'
        self.pin=issuer.initialize(directory,'site',self.password,self.now)
        self.server_serial=self.enroll('camera',directory=directory)
        self.client_serial=self.enroll('ingest',directory=directory)
        self.bundle=self.root/'new-bundle'
        issuer.export(directory,self.pin,self.password,self.bundle,self.now)
        self.server.close(); await self.server.wait_closed()
        self.server=await asyncio.start_server(self.accept,'127.0.0.1',self.port,ssl=self.context(),ssl_handshake_timeout=1)
        await self.rejected()
        path=self.prepare(self.root/'new-root-stage')
        await self.start(path); await self.until(lambda:self.marker.exists() and self.marker.stat().st_size>0)
        await self.until(lambda:self.publisher_marker.exists() and self.publisher_marker.stat().st_size>0)
        pids=[int(self.marker.read_text()),int(self.publisher_marker.read_text())]
        await self.stop()
        for pid in pids:
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)
        self.marker.unlink(); self.publisher_marker.unlink(); self.requests=0
        self.server.close(); await self.server.wait_closed()
        self.server=await asyncio.start_server(self.accept,'127.0.0.1',self.port,ssl=self.ssl_context,ssl_handshake_timeout=1)
        await self.rejected(path=path)

    async def test_disconnected_handshake_retries_without_encoder_or_http(self):
        await self.plain_peer()
        self.drop_handshake=True
        await self.start(); await self.until(lambda:self.connections>=2,seconds=4)
        self.assertIsNone(self.process.returncode)
        self.assertEqual(self.peak,1); self.assertFalse(self.marker.exists()); self.assertEqual(self.requests,0)
        await self.stop()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ingest',required=True,type=Path)
    args=parser.parse_args(); BINARY=args.ingest.resolve()
    issuer.qualified_runtime()
    unittest.main(argv=[sys.argv[0]],verbosity=2)
