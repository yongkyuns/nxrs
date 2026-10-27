"""Private synthetic authority and actual TLS client for SDK server controls.

Never print keys, DER, payload or issuer state. No live services or key-log files.
"""
import argparse
from datetime import timedelta
import hashlib
import importlib.util
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('actual_tls_issuer',ROOT/'tools/enrollment/issuer.py')
issuer=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(issuer)
RESPONSE=b'nxrs actual SDK mutual TLS response\n'
REQUEST=b'GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'

def write(directory,name,data):
    fd=os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(data)

def prepare(directory):
    now=issuer.instant();password=secrets.token_hex(48).encode('ascii')
    authority=directory/'issuer';pin=issuer.initialize(authority,'site_a',password,now)
    catalog=dict(cameras=[dict(camera_id='cam_a',site_id='site_a',gateway_id='gw_a')])
    server_serial=issuer.issue(authority,pin,password,(directory/'request.csr').read_bytes(),catalog,'cam_a','camera','127.0.0.1',now=now)
    client_key=ec.generate_private_key(ec.SECP256R1())
    client_request=(x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,'advisory-ingest')])).sign(client_key,hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM))
    client_serial=issuer.issue(authority,pin,password,client_request,catalog,'cam_a','ingest',None,now=now)
    data=issuer.parse((authority/issuer.STATE).read_bytes());root=issuer.certificate(data['ca_certificate'])
    server=issuer.certificate(next(e['certificate'] for e in data['entries'] if e['serial']==server_serial))
    client=issuer.certificate(next(e['certificate'] for e in data['entries'] if e['serial']==client_serial))
    ca_key=issuer.issuer_key(data,root,password)
    write(directory,'root.der',root.public_bytes(serialization.Encoding.DER));write(directory,'root.pem',root.public_bytes(serialization.Encoding.PEM))
    write(directory,'server.der',server.public_bytes(serialization.Encoding.DER));write(directory,'pin.bin',bytes.fromhex(pin))
    write(directory,'now',str(int(now.timestamp())).encode('ascii'))
    short_server=(x509.CertificateBuilder().subject_name(server.subject).issuer_name(server.issuer)
        .public_key(server.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(server.not_valid_before_utc).not_valid_after(now+timedelta(seconds=60)))
    for extension in server.extensions:
        short_server=short_server.add_extension(extension.value,extension.critical)
    write(directory,'short-server.der',short_server.sign(ca_key,hashes.SHA256()).public_bytes(serialization.Encoding.DER))
    write(directory,'valid.pem',client.public_bytes(serialization.Encoding.PEM))
    write(directory,'client-key.pem',client_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    def variant(name,*,changes=None,start=None,end=None,algorithm=None,signer=None,issuer_name=None):
        builder=(x509.CertificateBuilder().subject_name(client.subject).issuer_name(issuer_name or client.issuer)
            .public_key(client.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start or client.not_valid_before_utc).not_valid_after(end or client.not_valid_after_utc))
        changes=dict(changes or {})
        for extension in client.extensions:
            value=changes.pop(extension.oid,(extension.value,extension.critical))
            if value is not None:builder=builder.add_extension(*value)
        for value in changes.values():builder=builder.add_extension(*value)
        write(directory,name+'.pem',builder.sign(signer or ca_key,algorithm or hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    for name,uri in [('wrong-camera','urn:nxrs:ingest:site_a:gw_a:other'),
                     ('wrong-gateway','urn:nxrs:ingest:site_a:other:cam_a'),('wrong-site','urn:nxrs:ingest:other:gw_a:cam_a')]:
        variant(name,changes={x509.SubjectAlternativeName.oid:(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]),False)})
    variant('wrong-role',changes={x509.ExtendedKeyUsage.oid:(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),False)})
    variant('missing-role',changes={x509.ExtendedKeyUsage.oid:None})
    variant('extra-role',changes={x509.ExtendedKeyUsage.oid:(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH,ExtendedKeyUsageOID.SERVER_AUTH]),False)})
    variant('extra-san',changes={x509.SubjectAlternativeName.oid:(x509.SubjectAlternativeName([
        x509.UniformResourceIdentifier('urn:nxrs:ingest:site_a:gw_a:cam_a'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False)})
    variant('missing-san',changes={x509.SubjectAlternativeName.oid:None})
    variant('ca-leaf',changes={x509.BasicConstraints.oid:(x509.BasicConstraints(True,0),True)})
    variant('expired',start=now-timedelta(days=2),end=now-timedelta(seconds=1))
    variant('future',start=now+timedelta(hours=1))
    variant('too-long',end=now+timedelta(days=31))
    variant('sha384',algorithm=hashes.SHA384())
    variant('short',end=now+timedelta(seconds=2))
    variant('unknown-ca',signer=ec.generate_private_key(ec.SECP256R1()),issuer_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'untrusted test authority')]))
    raw=bytearray(client.public_bytes(serialization.Encoding.DER));raw[-1]^=1
    import base64
    encoded=base64.b64encode(raw)
    write(directory,'bad-signature.pem',b'-----BEGIN CERTIFICATE-----\n'+b'\n'.join(encoded[i:i+64] for i in range(0,len(encoded),64))+b'\n-----END CERTIFICATE-----\n')
    def export(name):
        target=directory/('bundle-'+name);issuer.export(authority,pin,password,target,now=now)
        write(directory,name+'.crl',x509.load_pem_x509_crl((target/'revocations.pem').read_bytes()).public_bytes(serialization.Encoding.DER))
    export('clear');export('newer');issuer.revoke(authority,pin,password,client_serial,now=now);export('revoked')
    assert hashlib.sha256((directory/'root.der').read_bytes()).hexdigest()==pin
    print('Disposable actual TLS client/issuer controls prepared')

def client(directory,port,label,version):
    # ssl.create_default_context can otherwise honor the caller's key-log path.
    os.environ.pop('SSLKEYLOGFILE',None)
    context=ssl.create_default_context(cafile=str(directory/'root.pem'))
    selected={'11':ssl.TLSVersion.TLSv1_1,'12':ssl.TLSVersion.TLSv1_2,'13':ssl.TLSVersion.TLSv1_3}[version]
    context.minimum_version=selected;context.maximum_version=selected
    context.set_ciphers('ECDHE-ECDSA-AES128-SHA:@SECLEVEL=0' if version=='11' else
                        ('ECDHE-ECDSA-AES128-GCM-SHA256' if label=='unsupported-suite' else 'ECDHE-ECDSA-CHACHA20-POLY1305'))
    if label!='no-certificate':context.load_cert_chain(directory/(('valid' if label in ['mjpeg','mjpeg-live','unsupported-suite'] else label)+'.pem'),directory/'client-key.pem')
    with socket.create_connection(('127.0.0.1',port),timeout=10) as raw:
        print('TLS client connected',flush=True)
        with context.wrap_socket(raw,server_hostname='127.0.0.1') as tls:
            tls.settimeout(10);tls.sendall(REQUEST)
            negotiated=(tls.version(),tls.cipher()[0])
            if label in ['mjpeg','mjpeg-live']:
                expected=Path(__file__).with_name('frame.jpg').read_bytes()
                assert 4<=len(expected)<=153600
                with tls.makefile('rb') as stream:
                    def headers():
                        lines=[];size=0
                        while True:
                            line=stream.readline(1025);size+=len(line)
                            assert line and len(line)<=1024 and size<=1024
                            if line==b'\r\n':return lines
                            assert line.endswith(b'\r\n');lines.append(line[:-2])
                    initial=headers()
                    assert initial[0]==b'HTTP/1.1 200 OK'
                    assert b'Content-Type: multipart/x-mixed-replace; boundary=nxrs-frame' in initial
                    previous=0
                    for _ in range(2):
                        assert stream.readline(32)==b'--nxrs-frame\r\n'
                        values=dict(line.split(b': ',1) for line in headers())
                        assert values[b'Content-Type']==b'image/jpeg'
                        assert int(values[b'Content-Length'])==len(expected)
                        sequence=int(values[b'X-Sequence']);assert sequence>previous;previous=sequence
                        assert stream.read(len(expected))==expected
                        assert stream.read(2)==b'\r\n'
                    if label=='mjpeg-live':
                        write(directory,'worker-mjpeg-ready',b'ready')
                        received=0
                        try:
                            while True:
                                chunk=stream.read1(4096)
                                if not chunk:break
                                received+=len(chunk);assert received<=1024*1024
                        except ssl.SSLError as error:
                            assert not isinstance(error,(ssl.SSLCertVerificationError,ssl.SSLWantReadError,ssl.SSLWantWriteError))

            else:
                output=bytearray()
                while len(output)<len(RESPONSE):
                    chunk=tls.recv(len(RESPONSE)-len(output));assert chunk;output.extend(chunk)
                assert output==RESPONSE
            assert negotiated[0]==('TLSv1.2' if version=='12' else 'TLSv1.3')
            assert negotiated[1]==('ECDHE-ECDSA-CHACHA20-POLY1305' if version=='12' else 'TLS_CHACHA20_POLY1305_SHA256')
    print('Actual mutually authenticated TLS client passed')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','client']);parser.add_argument('directory',type=Path)
    parser.add_argument('--port',type=int);parser.add_argument('--case',default='valid');parser.add_argument('--version',choices=['11','12','13'],default='13')
    args=parser.parse_args();os.umask(0o077)
    if args.mode=='prepare':prepare(args.directory)
    else:
        assert 0<args.port<65536 and args.case in ['valid','mjpeg','mjpeg-live','unsupported-suite','no-certificate','wrong-camera','wrong-gateway','wrong-site','wrong-role',
            'missing-role','extra-role','extra-san','missing-san','ca-leaf','expired','future','too-long','sha384','bad-signature','short','unknown-ca']
        client(args.directory,args.port,args.case,args.version)
if __name__=='__main__':main()
