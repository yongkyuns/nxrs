"""Private host fixture for the real SDK key/TLS endpoint; no forwarding proxy.

Only synthetic images, OS entropy, offline issuer custody and initial trusted
clock/floor are fixtures. This does not qualify physical/MCU key or time sources.
"""
import asyncio
import os
from pathlib import Path
import secrets
import sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from rust_camera import RustCamera, generate_images
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools/enrollment'))
import issuer
import prepare_ingest

def private(path, data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as output:output.write(data)

def issuer_password():
    # The issuer accepts bytes but forbids NUL/newline in passphrase input.
    # Hex preserves all 384 random bits while satisfying that text boundary.
    return secrets.token_hex(48).encode('ascii')

class SdkCamera(RustCamera):
    @classmethod
    async def start(cls,binary,directory,catalog_path,*,camera_id='cam_reference',motion=False,images=None):
        issuer.qualified_runtime()
        root=Path(directory);fixture=root/'sdk-camera-private';fixture.mkdir(mode=0o700)
        catalog=issuer.parse(issuer.read_file(Path(catalog_path).resolve(),65536))
        camera=next(c for c in catalog['cameras'] if c['camera_id']==camera_id)
        private(fixture/'scope',('\n'.join([camera['site_id'],camera['gateway_id'],camera_id])+'\n').encode())
        if images is None: generate_images(root,motion=motion)
        else:
            if not isinstance(images,tuple) or len(images)!=2 or not all(isinstance(image,bytes) and 4<=len(image)<=153600 for image in images):
                raise ValueError('bounded public image fixtures required')
            for index,image in enumerate(images,1):private(root/f'rust-frame-{index}.jpg',image)
        environment=dict(os.environ,NXRS_DEVICE_TLS_FIXTURE_DIR=str(fixture))
        environment.pop('SSLKEYLOGFILE',None)
        log=os.open(root/'sdk-camera-private.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(log,'wb') as errors:
            process=await asyncio.create_subprocess_exec(str(binary),str(root/'rust-frame-1.jpg'),str(root/'rust-frame-2.jpg'),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=errors,env=environment,limit=1024)
        self=cls(process);self.root=root;self.catalog_path=Path(catalog_path).resolve();self.camera_id=camera_id
        try:
            if await self._read()!={'state':'csr-ready'}:raise AssertionError('SDK-generated CSR readiness')
            password=issuer_password();authority=fixture/'issuer';now=issuer.instant()
            self._issuer_password=password;self._authority=authority;self._issuer_now=now;self._updates=0
            self._fixture=fixture
            self.pin=issuer.initialize(authority,camera['site_id'],password,now)
            camera_serial=issuer.issue(authority,self.pin,password,issuer.read_file((fixture/'device.csr').resolve(),2048),
                catalog,camera_id,'camera','127.0.0.1',now=now)
            key=ec.generate_private_key(ec.SECP256R1())
            request=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key,hashes.SHA256())
            client_serial=issuer.issue(authority,self.pin,password,request.public_bytes(serialization.Encoding.PEM),catalog,camera_id,'ingest',None,now=now)
            self.keys={'ingest':fixture/'ingest.key'}
            private(self.keys['ingest'],key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            self.serials={'camera':camera_serial,'ingest':client_serial};self.bundle=fixture/'public-bundle'
            issuer.export(authority,self.pin,password,self.bundle,now=now)
            verified=issuer.verify_bundle((self.bundle/'bundle.json').read_bytes(),self.pin,1,now=now)
            private(fixture/'pin.bin',bytes.fromhex(self.pin))
            private(fixture/'root.der',issuer.certificate((self.bundle/'ca.pem').read_bytes()).public_bytes(serialization.Encoding.DER))
            private(fixture/'camera.der',issuer.certificate((self.bundle/('cert-'+camera_serial+'.pem')).read_bytes()).public_bytes(serialization.Encoding.DER))
            self.initial_crl=x509.load_pem_x509_crl((self.bundle/'revocations.pem').read_bytes()).public_bytes(serialization.Encoding.DER)
            private(fixture/'revocations.der',self.initial_crl)
            private(fixture/'generation',str(verified['generation']).encode())
            private(fixture/'utc',str(int(now.timestamp())).encode())
            process.stdin.write(b'enrolled\n');await asyncio.wait_for(process.stdin.drain(),5)
            ready=await self._read()
            if ready.get('implementation')!='rust-sdk-tls-stream' or not 1<=ready.get('port',0)<=65535:
                raise AssertionError('actual SDK TLS endpoint readiness')
            self.port=ready['port'];return self
        except BaseException:
            if process.returncode is None:process.kill()
            await asyncio.wait_for(process.wait(),5)
            raise

    def stage(self,baseline):
        return prepare_ingest.prepare(self.root/'sdk-camera-tls-stage',self.bundle/'bundle.json',self.pin,1,
            self.catalog_path,baseline,self.camera_id,self.serials['camera'],self.serials['ingest'],self.keys['ingest'])


    @staticmethod
    def validate_control(value):
        keys=['captured','control_generation','control_applied','control_refused','control_refreshes']
        if not all(type(value.get(key)) is int and 0<=value[key]<=2**63-1 for key in keys):
            raise AssertionError('bounded SDK owner control counters')
        if value.get('control_result') not in ['ready','applied','refused','refreshed','fatal']:
            raise AssertionError('fixed SDK owner control result')
        return value

    async def command(self,value):
        if value not in ['status','pause','resume','quit','refresh']:
            raise ValueError('invalid SDK fixture command')
        async with self.lock:return self.validate_control(await self._exchange(value))

    async def apply_crl(self,value):
        if not isinstance(value,bytes) or not 0<len(value)<=32768:raise ValueError('bounded SDK CRL control required')
        async with self.lock:
            path=self._fixture/'next-crl.der'
            private(path,value)
            try:return self.validate_control(await self._exchange('trust'))
            finally:path.unlink(missing_ok=True)

    async def advance_trust(self,*,revoke=False):
        # Disposable offline test authority only. Device receives public signed
        # CRLs; no CA key/password crosses the control channel. The initial UTC
        # fixture anchors issuance, avoiding host/device fractional-clock skew.
        if revoke:
            issuer.revoke(self._authority,self.pin,self._issuer_password,self.serials['ingest'],now=self._issuer_now)
        self._updates+=1;bundle=self._fixture/f'update-{self._updates}'
        issuer.export(self._authority,self.pin,self._issuer_password,bundle,now=self._issuer_now)
        crl=x509.load_pem_x509_crl((bundle/'revocations.pem').read_bytes()).public_bytes(serialization.Encoding.DER)
        return await self.apply_crl(crl)
