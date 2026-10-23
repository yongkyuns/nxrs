#!/usr/bin/env python3
"""Seal four bounded private error tails to the approved ephemeral debug key.

The private key remains off-repository. This disposable diagnostic cannot qualify
an image or replace production credential storage/transport. No network access.
"""
import argparse, base64, hashlib, json, os, stat, subprocess, sys
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
NAMES=('logs/build-0.out','logs/build-0.err','logs/build-1.out','logs/build-1.err')
TAIL=8192
BLOCK=256

def label(checkout,name,index,count):
    return b'nxrs-private-build-diagnostic-v1\0'+json.dumps([checkout,name,index,count],separators=(',',':')).encode('ascii')

def read_tail(root,name):
    first,second=name.split('/')
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        try: child=os.open(first,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
        except FileNotFoundError:return None
        try:
            try: fd=os.open(second,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=child)
            except FileNotFoundError:return None
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):raise ValueError('not regular')
                stream.seek(max(0,info.st_size-TAIL));raw=stream.read(TAIL+1)
                if len(raw)>TAIL:raise ValueError('tail limit')
                return raw
        finally:os.close(child)
    finally:os.close(directory)

def seal(root,public,checkout):
    key=serialization.load_pem_public_key(public)
    if not isinstance(key,rsa.RSAPublicKey) or key.key_size!=3072:raise ValueError('key refused')
    if len(checkout)!=40 or any(c not in '0123456789abcdef' for c in checkout):raise ValueError('checkout refused')
    files={}
    for name in NAMES:
        raw=read_tail(root,name)
        if raw is None:continue
        chunks=[raw[i:i+BLOCK] for i in range(0,len(raw),BLOCK)]
        files[name]=[base64.b64encode(key.encrypt(chunk,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=label(checkout,name,index,len(chunks))))).decode('ascii') for index,chunk in enumerate(chunks)]
    if not files:raise ValueError('no diagnostic inputs')
    result=dict(schema=1,scope='encrypted-build-diagnostic-only',qualified=False,checkout=checkout,public_key_sha256=hashlib.sha256(public).hexdigest(),max_tail_bytes=TAIL,files=files,seed_files_included=False,firmware_images_included=False,deployment_permitted=False)
    if len(json.dumps(result))>96*1024:raise ValueError('capsule limit')
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--public-key',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    try:
        import cryptography
        if cryptography.__version__!='50.0.2':raise ValueError('runtime refused')
        checkout=subprocess.check_output(['git','rev-parse','HEAD'],text=True,timeout=10).strip()
        result=seal(args.root,args.public_key.read_bytes(),checkout)
        with args.output.open('xb') as output:output.write(json.dumps(result,sort_keys=True,separators=(',',':')).encode('ascii')+b'\n')
        print('Bounded failure report sealed; no seed/image files or plaintext published')
    except Exception:
        print('Private diagnostic sealing refused',file=sys.stderr);return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
