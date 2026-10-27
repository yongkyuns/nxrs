"""TEST-ONLY TLS front end for the actual Rust stream-service fixture.

The gateway-to-fixture connection uses real mutual TLS; inside this host fixture
an authenticated, single owner forwards bytes to the unmodified Rust server on
loopback. This does not implement or qualify board TLS/entropy/handshake budgets.
"""
import asyncio
import hashlib
from pathlib import Path
import ssl
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools/enrollment'))
import issuer
import prepare_ingest


def private(path,data):
    import os
    with open(path,'xb',opener=lambda name,flags:os.open(name,flags,0o600)) as file:
        file.write(data if isinstance(data,bytes) else data.encode())


class TlsCameraBridge:
    @classmethod
    async def start(cls,root,source,catalog_path,camera_id='cam_reference'):
        issuer.qualified_runtime()
        self=cls();self.root=Path(root);self.source=source;self.catalog_path=Path(catalog_path)
        self.camera_id=camera_id;self.tasks=set();self.active=False;self.peak=0
        catalog=issuer.parse(issuer.read_file(self.catalog_path,65536))
        camera=next(c for c in catalog['cameras'] if c['camera_id']==camera_id)
        self.expected=f"urn:nxrs:ingest:{camera['site_id']}:{camera['gateway_id']}:{camera_id}"
        password=b'synthetic-TLS-fixture-issuer-only-not-a-production-secret'
        directory=self.root/'camera-test-issuer'
        self.pin=issuer.initialize(directory,camera['site_id'],password)
        self.keys={};self.serials={}
        for role in ['camera','ingest']:
            key=ec.generate_private_key(ec.SECP256R1())
            csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key,hashes.SHA256())
            serial=issuer.issue(directory,self.pin,password,csr.public_bytes(serialization.Encoding.PEM),catalog,
                                camera_id,role,'127.0.0.1' if role=='camera' else None)
            path=self.root/('test-'+role+'.key')
            private(path,key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            self.serials[role]=serial;self.keys[role]=path
        self.bundle=self.root/'camera-test-public'
        issuer.export(directory,self.pin,password,self.bundle)
        self.client_digest=issuer.certificate((self.bundle/('cert-'+self.serials['ingest']+'.pem')).read_bytes()).fingerprint(hashes.SHA256())
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.bundle/('cert-'+self.serials['camera']+'.pem'),self.keys['camera'])
        trust=self.root/'test-camera-ca-crl.pem'
        private(trust,(self.bundle/'ca.pem').read_bytes()+(self.bundle/'revocations.pem').read_bytes())
        ctx.load_verify_locations(cafile=trust);ctx.verify_mode=ssl.CERT_REQUIRED
        ctx.verify_flags|=ssl.VERIFY_X509_STRICT|ssl.VERIFY_CRL_CHECK_LEAF
        self.server=await asyncio.start_server(self.accept,'127.0.0.1',0,ssl=ctx,ssl_handshake_timeout=2,limit=8192)
        self.port=self.server.sockets[0].getsockname()[1]
        return self

    def stage(self,baseline):
        return prepare_ingest.prepare(self.root/'camera-tls-stage',self.bundle/'bundle.json',self.pin,1,
            self.catalog_path,baseline,self.camera_id,self.serials['camera'],self.serials['ingest'],self.keys['ingest'])

    async def accept(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task)
        upstream=None;copies=[];owned=False
        try:
            peer=writer.get_extra_info('peercert')
            actual=hashlib.sha256(writer.get_extra_info('ssl_object').getpeercert(binary_form=True)).digest()
            if actual!=self.client_digest or tuple(peer.get('subjectAltName',()))!=(('URI',self.expected),) or self.active:
                return
            self.active=True;owned=True;self.peak=max(self.peak,1)
            source,upstream=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',self.source.port,limit=8192),2)
            async def copy(input,output):
                while data:=await input.read(8192):
                    output.write(data);await asyncio.wait_for(output.drain(),3)
            copies=[asyncio.create_task(copy(reader,upstream)),asyncio.create_task(copy(source,writer))]
            await asyncio.wait(copies,return_when=asyncio.FIRST_COMPLETED)
        except (OSError,asyncio.TimeoutError): pass
        finally:
            for copy in copies: copy.cancel()
            await asyncio.gather(*copies,return_exceptions=True)
            for output in [upstream,writer]:
                if output is not None:
                    output.close()
                    try: await asyncio.wait_for(output.wait_closed(),2)
                    except (OSError,asyncio.TimeoutError): pass
            if owned:self.active=False
            self.tasks.discard(task)

    async def close(self):
        self.server.close();await self.server.wait_closed()
        tasks=list(self.tasks)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.tasks:raise AssertionError('TLS fixture did not retire')
