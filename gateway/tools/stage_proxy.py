#!/usr/bin/env python3
"""Prepare a NEW private proxy review bundle; never approve, install or start it."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import candidate
import gateway_proxy_evidence as evidence

LIBRARIES=dict(loader='/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2',
    libc='/usr/lib/x86_64-linux-gnu/libc.so.6',crypto='/usr/lib/x86_64-linux-gnu/libcrypto.so.3',
    ssl='/usr/lib/x86_64-linux-gnu/libssl.so.3')


def prepare(destination,runtime=evidence.RUNTIME):
    evidence.require(os.geteuid()!=0,'stage-as-unprivileged-reviewer')
    # Git and source/receipt verification are read-only. This is local preparation,
    # not a source of release authorization or a replacement for operator review.
    def git(*args):
        return subprocess.run(['/usr/bin/git','-C',str(ROOT),*args],check=True,capture_output=True,timeout=5).stdout.decode().strip()
    evidence.require(not git('status','--porcelain'),'clean-proxy-review-checkout')
    head=git('rev-parse','HEAD');inputs=candidate.source_inputs()
    proof=evidence.verify(ROOT,head,inputs,runtime)
    evidence.require(set(proof['libraries'])==set(LIBRARIES.values()),'exact-supported-proxy-libraries')
    pin=json.loads((ROOT/'security/gateway-proxy-dependencies.json').read_bytes());review=pin['advisory_review']
    for name in ['reviewed_at','valid_until']:
        evidence.require(review[name+'_unix']==int(datetime.fromisoformat(review[name]).timestamp()),'consistent-proxy-review-bounds')
    policy=dict(schema=1,scope='operator-approved-minimal-camera-proxy',
        source_policy_sha256=evidence.digest(ROOT/'security/gateway-proxy-dependencies.json'),
        qualification_sha256=proof['qualification_sha256'],reviewed_at=review['reviewed_at_unix'],valid_until=review['valid_until_unix'],
        binary_sha256=proof['binary_sha256'],libraries={k:proof['libraries'][p] for k,p in LIBRARIES.items()})
    # mkdir is exclusive even for a dangling symlink. Partial failures retain an
    # unusable review directory; never replace/reuse a previous reviewed artifact.
    destination.mkdir(mode=0o700)
    target=destination/'nginx'
    with open(runtime/'bin/nginx','rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as src, \
            open(target,'xb',opener=lambda p,f:os.open(p,f,0o700)) as dst:
        total=0
        while raw:=src.read(1024*1024):
            total+=len(raw);evidence.require(total<=64*1024*1024,'bounded-proxy-copy');dst.write(raw)
    evidence.require(evidence.digest(target)==proof['binary_sha256'],'same-staged-proxy')
    raw,_=evidence.read(runtime/'qualification.json')
    evidence.require(hashlib.sha256(raw).hexdigest()==proof['qualification_sha256'],'same-staged-proxy-receipt')
    (destination/'qualification.json').write_bytes(raw);(destination/'qualification.json').chmod(0o600)
    evidence.require(proof==evidence.verify(ROOT,head,inputs,runtime) and inputs==candidate.source_inputs()
        and head==git('rev-parse','HEAD') and not git('status','--porcelain'),'unchanged-proxy-review-inputs')
    raw=(json.dumps(policy,indent=2,sort_keys=True)+'\n').encode()
    with open(destination/'review.json','xb',opener=lambda p,f:os.open(p,f,0o600)) as out:out.write(raw)
    return dict(review_sha256=hashlib.sha256(raw).hexdigest(),qualification_sha256=proof['qualification_sha256'],
        binary_sha256=proof['binary_sha256'],operator_approval_required=True,deployment_permitted=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    try:result=prepare(parser.parse_args().output)
    except Exception as error:
        category=str(error) if isinstance(error,ValueError) and str(error).replace('-','').isalnum() else type(error).__name__
        parser.exit(1,'Proxy review preparation refused ('+category+').\n')
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
