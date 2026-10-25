#!/usr/bin/env python3
"""Exact x264 parameter cleanup backport and bounded hosted sanitizer controls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
BEFORE=b'fail:\n    x264_free( h );\n    return NULL;\n}'
AFTER=b'fail:\n    if( h != NULL )\n        x264_param_cleanup( &h->param );\n    x264_free( h );\n    return NULL;\n}'
COUNTS=dict(constructor_refusals=64,successful_encodes=2,param_free_calls=16)


def require(value,category):
    if not value:raise ValueError(category)


def digest(raw):return hashlib.sha256(raw).hexdigest()


def recipe():return json.loads((ROOT/'tools/gateway-media/dependencies.json').read_bytes())['x264']['cleanup']


def apply(source):
    pin=recipe();patch=ROOT/pin['patch']
    require(digest(patch.read_bytes())==pin['patch_sha256'],'reviewed-x264-cleanup-patch')
    path=Path(source)/'encoder/encoder.c'
    require(not path.is_symlink() and path.is_file(),'regular-x264-encoder-source')
    before=path.read_bytes()
    require(digest(before)==pin['before_sha256'] and before.count(BEFORE)==1,'exact-x264-cleanup-base')
    after=before.replace(BEFORE,AFTER,1)
    require(digest(after)==pin['after_sha256'],'exact-x264-cleanup-result')
    path.write_bytes(after)
    return dict(applied=True,**pin)


def observation(raw,status,*,baseline):
    # Native sanitizer output remains private. Export only fixed counters and
    # whether the required native leak control was actually observed.
    lines=raw.decode('ascii',errors='replace').splitlines()
    markers=[v[len('X264_OPEN_CLEANUP '):] for v in lines if v.startswith('X264_OPEN_CLEANUP ')]
    require(len(markers)==1 and len(markers[0])<=256,'x264-native-controls-complete')
    counts=json.loads(markers[0]);require(counts==COUNTS and all(type(v) is int for v in counts.values()),'x264-native-control-counts')
    summary=re.findall(r'SUMMARY: AddressSanitizer: ([0-9]+) byte\(s\) leaked in ([0-9]+) allocation\(s\)\.',raw.decode('ascii',errors='replace'))
    other=(b'ERROR: AddressSanitizer:' in raw or b'runtime error:' in raw)
    if baseline:
        require(status==86 and len(summary)==1 and int(summary[0][0])>=166
                and int(summary[0][1])>=3 and b'ERROR: LeakSanitizer: detected memory leaks' in raw
                and not other,'actual-unpatched-x264-leak-required')
        return dict(**counts,leak_detected=True,leaked_bytes=int(summary[0][0]),leaked_allocations=int(summary[0][1]))
    require(status==0 and not summary and b'LeakSanitizer:' not in raw and not other,'clean-patched-x264-controls-required')
    return dict(**counts,leak_detected=False)


def run(work,report):
    import prepare
    prepare.native.hosted() # Before files, processes, network or build operations.
    work=work.resolve();report=report.resolve()
    require(os.geteuid()!=0 and not os.path.lexists(work) and not os.path.lexists(report),'new-unprivileged-x264-controls')
    before=prepare.inputs();head=prepare.native.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not prepare.native.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-x264-controls-source')
    work.mkdir(mode=0o700);os.umask(0o077)
    pin=json.loads((ROOT/'tools/gateway-media/dependencies.json').read_bytes())['x264']
    runner=prepare.load('x264_cleanup_process','gateway/fuzz/run.py')
    env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','HOME':str(work),
        'ASAN_OPTIONS':'detect_leaks=1:abort_on_error=0:halt_on_error=1',
        'LSAN_OPTIONS':'exitcode=86','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=0'}
    def execute(args,phase,cwd,seconds=180,success=True):
        status,raw=runner.execute(args,work,phase,env,seconds,cwd=cwd)
        require(len(raw)<=1024*1024,'bounded-x264-native-output')
        if success:require(status==0,'x264-control-'+phase)
        return status,raw
    compiler=Path('/usr/bin/clang').resolve();cc=dict(sha256=prepare.digest(compiler),version=prepare.native.command([str(compiler),'--version']).splitlines()[0])
    flags=['-O1','-g','-fno-omit-frame-pointer','-fsanitize=address,undefined','-fno-sanitize-recover=all']
    variants={}
    for name in ['unpatched','patched']:
        folder=work/name;folder.mkdir(mode=0o700)
        source=prepare.fetch(pin,folder,'x264')
        if name=='patched':apply(source)
        execute([source/'configure','--cc='+str(compiler),'--enable-static','--disable-shared','--disable-cli','--disable-opencl',
            '--disable-asm','--bit-depth=all','--extra-cflags='+' '.join(flags),
            '--extra-ldflags=-fsanitize=address,undefined'],'configure-'+name,source)
        execute(['make','-j2','lib-static'],'build-'+name,source)
        binary=folder/'x264-open-cleanup'
        execute([compiler,*flags,'-I',source,ROOT/'tests/host/native/x264_open_cleanup.c',source/'libx264.a',
            '-lm','-lpthread','-ldl','-o',binary],'link-'+name,source)
        status,raw=execute([binary],'execute-'+name,folder,seconds=30,success=False)
        variants[name]=dict(observation(raw,status,baseline=name=='unpatched'),binary_sha256=prepare.digest(binary),
            library_sha256=prepare.digest(source/'libx264.a'),encoder_source_sha256=prepare.digest(source/'encoder/encoder.c'))
    require(before==prepare.inputs() and prepare.digest(compiler)==cc['sha256'],'unchanged-x264-controls-inputs')
    require(head==prepare.native.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
        and not prepare.native.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-clean-x264-checkout')
    result=dict(schema=1,scope='host-x264-encoder-open-parameter-cleanup-controls',qualified=True,
        checkout=dict(commit=head,clean=True),inputs=before,source=pin,compiler=cc,variants=variants,
        native_c_asan_ubsan_lsan=True,bit_depths=[8,10],synthetic_valid_frame_encoded=True,
        production_assembly_qualified=False,all_encoder_failure_paths_qualified=False,
        upstream_advisory_closure=False,full_native_advisory_closure=False,physical_qualified=False,
        fips_validated=False,deployment_permitted=False)
    raw=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode();require(len(raw)<=65536,'bounded-x264-controls-receipt')
    with report.open('xb') as output:output.write(raw)
    print('PASS: actual unpatched leak and patched 8/10-bit constructor/ownership/encode controls; broader gates remain open')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    try:run(**vars(p.parse_args()))
    except Exception as error:
        category=str(error) if isinstance(error,ValueError) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        p.exit(1,'x264 cleanup qualification refused ('+category+').\n')


if __name__=='__main__':main()
