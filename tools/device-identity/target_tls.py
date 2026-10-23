"""Fresh host UTC/CA fixture for actual target TLS; never production trust.

Root, pin and initial UTC are fixed before compilation. Console bytes cannot
replace them. The 2030 returned-certificate fixture remains a separate strict
profile; an ordinary host TLS peer can verify this fresh fixture without a
verification bypass or changing any system clock.
"""
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import serialization
ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('actual_target_tls_issuer',ROOT/'tools/enrollment/issuer.py')
issuer=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(issuer)
SCOPE='fresh-host-tls-loopback-fixture-only'

def require(value):
    if not value:raise ValueError('target TLS fixture refused')
def digest(raw):return hashlib.sha256(raw).hexdigest()
def fresh(path,data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data)
def read(directory,name,limit):return issuer.read_file(Path(directory)/name,limit)

def public_bindings(directory):
    root,pin,clock=(read(directory,name,limit) for name,limit in [('root.der',2048),('pin.bin',32),('time.bin',8)])
    manifest=issuer.parse(read(directory,'trust.json',512))
    require(set(manifest)=={'scope','root_der_sha256','trusted_unix_seconds'} and manifest['scope']==SCOPE)
    require(len(pin)==32 and len(clock)==8 and digest(root)==pin.hex()==manifest['root_der_sha256'])
    unix=int.from_bytes(clock,'little')
    require(type(manifest['trusted_unix_seconds']) is int and unix==manifest['trusted_unix_seconds']
            and 0<unix<=253402300799 and abs(int(issuer.instant().timestamp())-unix)<=86400)
    now=datetime.fromtimestamp(unix,issuer.UTC)
    ca=x509.load_der_x509_certificate(root);issuer.p256(ca.public_key());issuer.cert_signature(ca,ca)
    require(ca.signature_hash_algorithm.name=='sha256' and ca.not_valid_before_utc<=now<ca.not_valid_after_utc
            and ca.extensions.get_extension_for_class(x509.BasicConstraints).value==x509.BasicConstraints(True,0))
    return dict(schema=1,scope=SCOPE,qualified=False,root_der_sha256=digest(root),pin_bytes_sha256=digest(pin),
                trusted_utc_bytes_sha256=digest(clock),trusted_unix_seconds=unix,
                time_source='private-fresh-host-qualification-fixture-only',trusted_time_acquisition_qualified=False,
                protected_generation_qualified=False,physical_qualified=False,deployment_permitted=False)

def prepare(directory):
    directory=Path(directory);directory.mkdir(mode=0o700)
    now=issuer.instant();password=os.urandom(48).hex().encode('ascii')
    pin=issuer.initialize(directory/'issuer','site_a',password,now)
    state=issuer.parse(read(directory/'issuer',issuer.STATE,issuer.MAX_STATE))
    root=issuer.certificate(state['ca_certificate']).public_bytes(serialization.Encoding.DER)
    unix=int(now.timestamp())
    for name,raw in [('password.bin',password),('root.der',root),('pin.bin',bytes.fromhex(pin)),('time.bin',unix.to_bytes(8,'little')),
        ('trust.json',issuer.canonical(dict(scope=SCOPE,root_der_sha256=digest(root),trusted_unix_seconds=unix)))]:fresh(directory/name,raw)
    return public_bindings(directory)

def issue(directory,request):
    """Sign the live target CSR and create the independent host client's key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    directory=Path(directory);proof=public_bindings(directory)
    now=datetime.fromtimestamp(proof['trusted_unix_seconds'],issuer.UTC)
    parsed=x509.load_pem_x509_csr(request)
    require(parsed.is_signature_valid and parsed.signature_hash_algorithm.name=='sha256')
    issuer.p256(parsed.public_key())
    require(parsed.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value=='cam_a')
    authority=directory/'issuer';password=read(directory,'password.bin',128);pin=proof['root_der_sha256']
    catalog=dict(cameras=[dict(camera_id='cam_a',site_id='site_a',gateway_id='gw_a')])
    server=issuer.issue(authority,pin,password,request,catalog,'cam_a','camera','127.0.0.1',now=now)
    key=ec.generate_private_key(ec.SECP256R1())
    csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key,hashes.SHA256())
    client=issuer.issue(authority,pin,password,csr.public_bytes(serialization.Encoding.PEM),catalog,'cam_a','ingest',None,now=now)
    bundle=directory/'bundle';issuer.export(authority,pin,password,bundle,now=now)
    issuer.verify_bundle(read(bundle,'bundle.json',issuer.MAX_STATE),pin,1,now=now)
    fresh(directory/'client.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    leaf=issuer.certificate(read(bundle,'cert-'+server+'.pem',4096)).public_bytes(serialization.Encoding.DER)
    crl=x509.load_pem_x509_crl(read(bundle,'revocations.pem',4096)).public_bytes(serialization.Encoding.DER)
    require(issuer.spki(issuer.certificate(read(bundle,'cert-'+server+'.pem',4096)).public_key())==issuer.spki(parsed.public_key()))
    require(issuer.certificate(read(bundle,'ca.pem',4096)).public_bytes(serialization.Encoding.DER)==read(directory,'root.der',2048))
    return leaf,crl,bundle/'cert-{}.pem'.format(client)

def terminal_peer_refusal(error):
    import ssl
    # OpenSSL versions expose different fatal-alert reason strings (including
    # an unlabelled record-layer failure). A local trust failure or ordinary
    # retry is not evidence that the native server rejected the peer. The
    # target must separately prove zero media/error/retirement before recovery.
    return isinstance(error,ssl.SSLError) and not isinstance(error,(
        ssl.SSLCertVerificationError,ssl.SSLWantReadError,ssl.SSLWantWriteError))

class Interaction:
    def __init__(self,directory,version='13'):
        self.directory=Path(directory);public_bindings(self.directory)
        require(version in ('12','13'));self.version=version;self.result=None
    def __call__(self,invocation,write,read_until):
        import os,re,ssl,time
        require(invocation=='identity_tls');write((invocation+'\n').encode())
        text=read_until(b'NXRS_TLS_CSR_END\n').replace('\r','')
        blocks=re.findall(r'^NXRS_TLS_CSR_BEGIN\n(.*?)^NXRS_TLS_CSR_END$',text,re.M|re.S)
        require(len(blocks)==1)
        leaf,crl,client=issue(self.directory,blocks[0].encode('ascii'))
        write(b'ENROLL\n')
        for value in (leaf,crl):
            write(('DER '+str(len(value))+'\n').encode())
            for offset in range(0,len(value),32):write(value[offset:offset+32].hex().encode()+b'\n')
        read_until(b'NXRS_TLS_ENROLLED\n')
        os.environ.pop('SSLKEYLOGFILE',None)
        context=ssl.create_default_context(cafile=str(self.directory/'bundle/ca.pem'))
        trust=read(self.directory/'bundle','ca.pem',4096)+read(self.directory/'bundle','revocations.pem',4096)
        trust_path=self.directory/'peer-ca-crl.pem';fresh(trust_path,trust)
        context.load_verify_locations(cafile=str(trust_path))
        require(context.cert_store_stats()['crl']==1)
        context.verify_flags|=ssl.VERIFY_X509_STRICT|ssl.VERIFY_CRL_CHECK_LEAF
        context.set_ciphers('ECDHE-ECDSA-AES128-GCM-SHA256')
        selected={'12':ssl.TLSVersion.TLSv1_2,'13':ssl.TLSVersion.TLSv1_3}[self.version]
        context.minimum_version=selected;context.maximum_version=selected
        incoming=ssl.MemoryBIO();outgoing=ssl.MemoryBIO()
        peer=context.wrap_bio(incoming,outgoing,server_hostname='127.0.0.1')
        counter=0;queued=0;received=0;sent=0;peer_eof=False;deadline=time.monotonic()+120
        def pump():
            nonlocal counter,queued,received,sent,peer_eof
            require(time.monotonic()<deadline)
            counter+=1;write(f'READ {counter}\n'.encode())
            raw=read_until(f'NXRS_TLS_FRAME_END {counter}\n'.encode()).replace('\r','')
            pattern=rf'^NXRS_TLS_FRAME {counter} ([0-9]+) pending=([0-9]+) eof=([01])\n(.*?)^NXRS_TLS_FRAME_END {counter}$'
            frames=re.findall(pattern,raw,re.M|re.S);require(len(frames)==1)
            length,pending,eof,hextext=frames[0];n=int(length);queued=int(pending)
            require(0<=n<=512 and 0<=queued<=4096)
            lines=hextext.splitlines();require(all(0<len(line)<=64 and len(line)%2==0 and re.fullmatch('[a-f0-9]+',line) for line in lines))
            value=bytes.fromhex(''.join(lines));require(len(value)==n)
            if value:incoming.write(value);received+=len(value)
            elif eof=='1' and not peer_eof:incoming.write_eof();peer_eof=True
            time.sleep(.03)
        def flush():
            nonlocal counter,sent
            while outgoing.pending:
                while queued:pump()
                value=outgoing.read(min(4096,outgoing.pending));counter+=1
                write(f'SEND {counter} {len(value)}\n'.encode())
                for offset in range(0,len(value),32):write(value[offset:offset+32].hex().encode()+b'\n')
                read_until(f'NXRS_TLS_SEND_ACCEPTED {counter}\n'.encode());sent+=len(value)
                pump()
        def handshake():
            while True:
                try:peer.do_handshake();flush();return
                except ssl.SSLWantReadError:flush();pump()
        request=b'GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'
        # First use a fully verifying client that deliberately has no client
        # certificate. TLS 1.3 may finish locally before receiving the server's
        # rejection, so also attempt application I/O and require zero plaintext.
        refused=False
        try:
            handshake();require(peer.write(request)==len(request));flush()
            while True:
                try:
                    value=peer.read(4096);require(not value);refused=True;break
                except ssl.SSLWantReadError:flush();pump()
        except ssl.SSLError as error:
            require(terminal_peer_refusal(error))
            refused=True
        require(refused)
        while not peer_eof:pump()
        write(b'RECONNECT\n');read_until(b'NXRS_TLS_RECONNECTED 1 refused_frames=0\n')
        incoming=ssl.MemoryBIO();outgoing=ssl.MemoryBIO();peer_eof=False;queued=0
        # The same target must then reject a raw HTTP request without emitting
        # media. The target independently checks zero completed frames after
        # each failed connection before admitting this reconnect.
        before=received;outgoing.write(request);flush()
        while not peer_eof:pump()
        raw=incoming.read();require(received-before<=64 and b'HTTP/1.' not in raw)
        write(b'RECONNECT\n');read_until(b'NXRS_TLS_RECONNECTED 2 refused_frames=0\n')
        context.load_cert_chain(client,self.directory/'client.key')
        incoming=ssl.MemoryBIO();outgoing=ssl.MemoryBIO();peer_eof=False;queued=0
        peer=context.wrap_bio(incoming,outgoing,server_hostname='127.0.0.1')
        started=time.monotonic();handshake()
        handshake_ms=round((time.monotonic()-started)*1000)
        require(peer.getpeercert(binary_form=True)==leaf)
        expected_version='TLSv1.2' if self.version=='12' else 'TLSv1.3'
        expected_cipher='ECDHE-ECDSA-AES128-GCM-SHA256' if self.version=='12' else 'TLS_AES_128_GCM_SHA256'
        require(peer.version()==expected_version and peer.cipher()[0]==expected_cipher)
        request=b'GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n';require(peer.write(request)==len(request));flush()
        plain=bytearray();expected=(ROOT/'tests/device-identity/frame.jpg').read_bytes()
        def take(n):
            while len(plain)<n:
                try:
                    value=peer.read(4096);require(bool(value));plain.extend(value);require(len(plain)<=32768)
                except ssl.SSLWantReadError:flush();pump()
            value=bytes(plain[:n]);del plain[:n];return value
        def headers():
            while b'\r\n\r\n' not in plain:
                # Ask for one more byte without consuming already buffered data.
                n=len(plain)+1;value=take(n);plain[:0]=value;require(len(plain)<=4096)
            end=plain.index(b'\r\n\r\n');value=take(end+4)
            return value[:-4].split(b'\r\n')
        response=headers();require(response[0]==b'HTTP/1.1 200 OK')
        require(b'Content-Type: multipart/x-mixed-replace; boundary=nxrs-frame' in response)
        previous=0
        for _ in range(2):
            require(take(len(b'--nxrs-frame\r\n'))==b'--nxrs-frame\r\n')
            fields=dict(line.split(b': ',1) for line in headers())
            require(int(fields[b'Content-Length'])==len(expected) and fields[b'Content-Type']==b'image/jpeg')
            sequence=int(fields[b'X-Sequence']);require(sequence>previous);previous=sequence
            require(take(len(expected))==expected and take(2)==b'\r\n')
        try:peer.unwrap()
        except ssl.SSLWantReadError:flush()
        write(b'QUIT\n')
        self.result=dict(tls_version=expected_version,cipher=expected_cipher,verified_frames=2,
            missing_client_certificate_refused=True,cleartext_refused=True,refused_media_frames=0,
            recovery_after_two_refusals=True,
            approved_server_der_sha256=digest(leaf),
            server_public_key_sha256=digest(issuer.spki(x509.load_der_x509_certificate(leaf).public_key())),
            handshake_host_wall_ms=handshake_ms,
            relayed_ciphertext_tx=sent,relayed_ciphertext_rx=received,qualified=False,
            physical_qualified=False,hardware_entropy_qualified=False,deployment_permitted=False)
