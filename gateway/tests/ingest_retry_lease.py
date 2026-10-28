#!/usr/bin/env python3
"""Run the actual Rust ingest with an offline camera and a short signed CRL lease.

Host process/lifecycle evidence only; no TLS connection or hardware qualification.
The generated private keys and bounded process output stay in the owned fixture.
"""
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/enrollment'))
import issuer


def source_inputs():
    names={'gateway/Cargo.toml','gateway/Cargo.lock','rust-toolchain.toml',
           'gateway/tests/ingest_retry_lease.py','tools/enrollment/issuer.py','gateway/fuzz/run.py',
           '.github/workflows/monitoring-ingest.yml'}
    for crate in ['gateway/ingest','gateway/private-file','gateway/media-process',
                  'gateway/media-sandbox','service/detection-wire']:
        names.add(crate+'/Cargo.toml')
        names.update(p.relative_to(ROOT).as_posix() for p in (ROOT/crate/'src').rglob('*.rs'))
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(names)}


def private(path,raw,mode=0o600):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode),'wb') as out:out.write(raw)


def fixture(root, address, expires):
    now=datetime.now(timezone.utc).replace(microsecond=0)
    name=x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME,'nxrs retry fixture only')])
    ca_key=ec.generate_private_key(ec.SECP256R1())
    ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
        .not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(True,0),critical=True)
        .add_extension(issuer.usage(True),critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False)
        .sign(ca_key,hashes.SHA256()))
    private(root/'ca.pem',ca.public_bytes(serialization.Encoding.PEM))
    for role in ['camera','ingest']:
        key=ec.generate_private_key(ec.SECP256R1())
        builder=(x509.CertificateBuilder().subject_name(x509.Name([
            x509.NameAttribute(x509.NameOID.COMMON_NAME,role+'_reference')])).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1)))
        entry=dict(role=role,camera_id='cam_reference',gateway_id='gw_reference',
                   ip='127.0.0.1' if role=='camera' else None)
        for extension,critical in issuer.leaf_extensions('site_reference',entry,key.public_key(),ca):
            builder=builder.add_extension(extension,critical)
        cert=builder.sign(ca_key,hashes.SHA256())
        private(root/(role+'.pem'),cert.public_bytes(serialization.Encoding.PEM))
        if role=='ingest':
            private(root/'ingest.key',key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    crl=(x509.CertificateRevocationListBuilder().issuer_name(name).last_update(now-timedelta(seconds=1))
        .next_update(expires).add_extension(x509.CRLNumber(1),critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False)
        .sign(ca_key,hashes.SHA256()))
    private(root/'revocations.pem',crl.public_bytes(serialization.Encoding.PEM))
    # These would write a marker if the offline source incorrectly spawned a
    # media process. They receive no fixture secrets in argv or environment.
    child=root/'unexpected-child'
    script=b'#!/bin/sh\n: > "'+str(child).encode()+b'"\nexit 1\n'
    private(root/'media-child',script,0o700)
    private(root/'publisher.token',b'c'*64)
    config=dict(camera=address,allow_insecure_camera_http=False,rtsp='127.0.0.1:1',stream_path='reference',publisher_user='publisher_reference',
        publisher_token_file=str(root/'publisher.token'),ffmpeg=str(root/'media-child'),
        publisher=str(root/'media-child'),width=320,height=240,frame_timeout_ms=2000,
        camera_tls=dict(site_id='site_reference',camera_id='cam_reference',gateway_id='gw_reference',
            issuer_sha256=hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest(),
            enrollment_generation=1,ca_certificate_file=str(root/'ca.pem'),
            camera_certificate_file=str(root/'camera.pem'),client_certificate_file=str(root/'ingest.pem'),
            client_key_file=str(root/'ingest.key'),crl_file=str(root/'revocations.pem')))
    private(root/'ingest.json',json.dumps(config).encode())


def exercise(binary):
    if os.name!='posix':raise ValueError('Unix host process qualification required')
    inputs=source_inputs()
    head=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],timeout=3).decode().strip()
    clean=not subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],timeout=3).strip()
    binary_digest=hashlib.sha256(binary.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='nxrs-retry-lease-') as name,socket.socket() as offline:
        root=Path(name).resolve()
        offline.bind(('127.0.0.1',0))
        address='127.0.0.1:'+str(offline.getsockname()[1])
        # Release the allocation so both Darwin and Linux return refusal.
        # A bound but non-listening Darwin socket can instead drop SYNs.
        # An unexpected replacement cannot authenticate with the pinned key;
        # the retry-count assertion also refuses a different failure schedule.
        offline.close()
        expires=datetime.now(timezone.utc).replace(microsecond=0)+timedelta(seconds=11)
        fixture(root,address,expires)
        command=[str(binary),str(root/'ingest.json'),'--require-camera-tls']
        check=subprocess.run([*command,'--check-config'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
        if check.returncode!=0:raise AssertionError('valid signed initial TLS policy required')
        # The original retry schedule attempts near 0,1,3,7,15 seconds. This
        # lease expires within the longest backoff, while no session is active.
        start=time.monotonic();remaining=expires.timestamp()-time.time()
        if not 9<=remaining<=11:raise AssertionError('fixture startup budget exceeded')
        spec=importlib.util.spec_from_file_location('retry_lease_process',ROOT/'gateway/fuzz/run.py')
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        previous=os.umask(0o077)
        try:
            # The qualified runner bounds output/time and retires the process
            # group before reaping its reserved leader, including timeout paths.
            status,raw=runner.execute(command,root,'process',{'PATH':'/usr/bin:/bin','LANG':'C'},remaining+2,cwd=ROOT)
        except ValueError:
            raise AssertionError('TLS lease failed to stop ingest within bounded reconnect evidence') from None
        finally:os.umask(previous)
        elapsed=time.monotonic()-start
        if (root/'unexpected-child').exists():raise AssertionError('offline ingest spawned a media child')
        if len(raw)>8192:raise AssertionError('bounded fixed-category process evidence required')
        if status!=1 or b'nxrs-camera-ingest: Authentication\n' not in raw:
            raise AssertionError('credential expiry must terminate with Authentication')
        if raw.count(b'retired source/encoder (Io); retrying')<4:
            raise AssertionError('actual ingest must reach its maximum reconnect backoff')
        if not remaining-.25<=elapsed<=remaining+2:
            raise AssertionError('bounded credential expiry timing required')
        if inputs!=source_inputs() or binary_digest!=hashlib.sha256(binary.read_bytes()).hexdigest():
            raise AssertionError('fixture source or binary inputs changed')
        return dict(schema=1,scope='host-rust-ingest-offline-crl-expiry',qualified=True,
            checkout=dict(commit=head,clean=clean),inputs=inputs,host_platform=sys.platform,
            valid_initial_policy=True,reached_maximum_backoff=True,expired_while_camera_offline=True,
            expiry_exit_category='Authentication',no_media_child_started=True,
            elapsed_seconds=round(elapsed,3),initial_lease_remaining_seconds=round(remaining,3),
            binary_sha256=binary_digest,
            tls_handshake_qualified=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--ingest',type=Path,required=True)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    value=exercise(args.ingest.resolve())
    if args.report:
        raw=(json.dumps(value,sort_keys=True)+'\n').encode()
        if len(raw)>65536:raise AssertionError('public retry receipt budget exceeded')
        with args.report.open('xb') as out:out.write(raw)
    print('PASS: real Rust ingest expires its signed CRL lease while offline in maximum reconnect backoff')


if __name__=='__main__':main()
