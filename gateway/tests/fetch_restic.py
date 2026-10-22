#!/usr/bin/env python3
"""Fetch one exact public backup-test artifact into a NEW file. No installation."""
import argparse
import bz2
import hashlib
import json
import os
from pathlib import Path
import urllib.request


def fetch(artifact,output):
    pins=json.loads((Path(__file__).parents[1]/'deploy/backup-dependencies.json').read_text())
    pin=pins[artifact]
    if output.exists():raise ValueError('new artifact output required')
    with urllib.request.urlopen(pin['url'],timeout=60) as source:data=source.read(40*1024*1024+1)
    if len(data)>40*1024*1024 or hashlib.sha256(data).hexdigest()!=pin['archive_sha256']:
        raise ValueError('backup artifact checksum or size')
    decoder=bz2.BZ2Decompressor();binary=decoder.decompress(data,max_length=64*1024*1024+1)
    if len(binary)>64*1024*1024 or not decoder.eof or decoder.unused_data or hashlib.sha256(binary).hexdigest()!=pin['binary_sha256']:
        raise ValueError('backup executable checksum or size')
    with open(output,'xb',opener=lambda p,f:os.open(p,f,0o700)) as destination:destination.write(binary)
    print('Verified restic artifact',pin['version'],artifact,pin['binary_sha256'])

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--artifact',choices=['linux_amd64','darwin_arm64'],required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();fetch(a.artifact,a.output)
