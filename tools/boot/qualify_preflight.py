#!/usr/bin/env python3
"""Qualify offline manufacturing preflight against a real pinned build and SDK."""
import argparse
import io
import json
import os
from pathlib import Path
import sys

from cryptography.hazmat.primitives import serialization
import espsecure
import preflight
import qualify as process
import sign
import target_plan as target

sys.path.insert(0, str(target.ROOT / 'tests/boot'))
from efuse_fixture import MAC, virtual_summary, rooted


def qualify(boot, policy, pin, platform, work):
    work = target.fresh(work)
    logs = work / 'logs'
    logs.mkdir(mode=0o700)
    names = ('tools/boot/preflight.py', 'tools/boot/qualify_preflight.py',
             'tools/boot/sign.py', 'tools/boot/target_plan.py', 'tools/boot/build_esp32s3.py',
             'tools/boot/qualify.py', 'tests/boot/efuse_fixture.py',
             'security/esp32s3-efuse-summary-schema.json', 'security/esp32s3-boot-dependencies.json')
    inputs = {name:process.digest(target.ROOT/name) for name in names}
    environment = {k:os.environ[k] for k in ('PATH','HOME','CARGO_HOME','RUSTUP_HOME','TMPDIR') if k in os.environ}
    environment['PYTHONNOUSERSITE'] = '1'
    # Fresh independent synthetic ROM public root. Its private key is never
    # serialized, logged, used to sign an artifact or installed on any device.
    key = sign.rsa.generate_private_key(public_exponent=65537, key_size=3072).public_key()
    public = work / 'rom-public.pem'
    public.write_bytes(key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
    digest = espsecure._digest_sbv2_public_key(io.BytesIO(public.read_bytes()))
    vendor_digest = work/'rom-digest.bin'
    status,_ = process.run([sys.executable,'-m','espsecure','digest-sbv2-public-key',
        '--keyfile',str(public),'--output',str(vendor_digest)],work,environment,logs,'vendor-digest',30)
    target.require(status == 0 and vendor_digest.read_bytes() == digest)
    build = boot / 'boot-build.json'
    args = argparse.Namespace(summary=work/'summary.json', summary_sha256=None,
        policy=policy, policy_sha256=pin, platform=platform,
        rom_root=public, rom_root_sha256=target.digest(key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)),
        expected_mac=MAC, boot_build=build, boot_build_sha256=process.digest(build),
        bootloader=boot/'build/mcuboot_esp32s3.bin', out=None)
    positives = []
    for label,raw,state in (
        ('unprovisioned',virtual_summary(),'unprovisioned-review-required'),
        ('staged',rooted(digest,False),'root-staged-review-required'),
        ('enabled',rooted(digest),'secure-boot-root-observed')):
        args.summary.write_bytes(raw)
        args.summary.chmod(0o600)
        args.summary_sha256 = target.digest(raw)
        args.out = work/(label+'.json')
        result = preflight.run(args)
        target.require(result['state'] == state and not result['deployment_permitted'] and not result['device_accessed'])
        positives.append(label)
    good = args.summary.read_bytes()

    def cli(name, output, raw, expected_status, expected_text):
        args.summary.write_bytes(raw)
        command = [sys.executable,str(target.ROOT/'tools/boot/preflight.py')]
        options = vars(args) | {'summary_sha256':target.digest(raw),'out':output}
        for key,value in options.items():
            command.extend(('--'+key.replace('_','-'),str(value)))
        status,text = process.run(command,work,environment,logs,name,30)
        target.require(status == expected_status)
        if expected_status:
            target.require(not output.exists() and (logs/(name+'.err')).read_text() == expected_text)
        else:
            target.require(text == expected_text and output.is_file())

    cli('cli-positive',work/'cli-positive.json',good,0,'Offline manufacturing assessment recorded; deployment remains unqualified\n')
    wrong = rooted(bytes([7])*32)
    cli('cli-other-root',work/'refused-root.json',wrong,1,'Preflight refused: unapproved-boot-root\n')
    forged = json.loads(good)
    forged['WR_DIS']['raw_value'] = '0x00000000'
    forged['WR_DIS']['value'] = 0
    cli('cli-forged-protection',work/'refused-protection.json',target.canonical(forged),1,
        'Preflight refused: summary-protection-registers\n')
    cli('cli-restored-positive',work/'restored.json',good,0,'Offline manufacturing assessment recorded; deployment remains unqualified\n')
    target.require(all(process.digest(target.ROOT/name) == sha for name,sha in inputs.items()))
    status,head = process.run(['git','rev-parse','HEAD'],target.ROOT,environment,logs,'repository-head',30)
    target.require(status == 0)
    status,changes = process.run(['git','status','--porcelain=v1','--untracked-files=normal'],target.ROOT,environment,logs,'repository-state',30)
    target.require(status == 0)
    report = dict(schema=1,scope='offline-preflight-actual-esp32s3-sdk-and-build-binding',qualified=True,
        repository_head=head.strip(),repository_has_changes=bool(changes),inputs=inputs,
        boot_build_sha256=process.digest(build),bootloader_sha256=process.digest(args.bootloader),
        actual_virtual_sdk_states=positives,actual_vendor_digest_cli=True,
        actual_cli_refusals=['other-root','forged-protection-registers'],restored_positive_passed=True,
        fixture='actual-vendor-memory-controller-read-register-injection',
        physical_capture=False,device_accessed=False,irreversible_actions_performed=False,
        rom_signature_qualified=False,coding_errors_qualified=False,physical_qualified=False,deployment_permitted=False)
    (work/'preflight-qualification.json').write_bytes(target.canonical(report)+b'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('boot','policy','work'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--policy-sha256',required=True)
    parser.add_argument('--platform',default='esp32s3-camera-mcuboot')
    args=parser.parse_args()
    try:
        qualify(args.boot,args.policy,args.policy_sha256,args.platform,args.work)
    except Exception:
        print('Offline manufacturing qualification refused',file=sys.stderr)
        return 1
    print('Offline manufacturing preflight qualified; physical gates remain open')
    return 0


if __name__=='__main__':raise SystemExit(main())
