#!/usr/bin/env python3
"""Inventory the executed minimized runtime and audit its actual Ubuntu origins."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import ubuntu

ROOT=ubuntu.ROOT
RUNTIME=Path('/usr/lib/nxrs-gateway-media-qualification')
SCOPE='hosted-minimized-gateway-native-origins-and-ubuntu-package-advisories'
INPUTS=('tools/native-packages/candidate.py','tests/host/test_candidate_native_packages.py',
        '.github/workflows/minimized-native-packages.yml','tools/native-packages/payloads.py',
        'tests/host/test_native_payloads.py','security/ubuntu-archive-trust.json')
sys.path.insert(0,str(ROOT/'gateway/tools'))
import gateway_media_evidence as media
import gateway_proxy_evidence as proxy
INPUTS=(*INPUTS,*proxy.INPUTS)


def source_inputs():
    names=set(ubuntu.source_inputs())|media.source_names(ROOT)|set(INPUTS)
    return {name:ubuntu.file_digest(ROOT/name) for name in sorted(names)}


def read(path):
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        ubuntu.require(stat.S_ISREG(os.fstat(source.fileno()).st_mode),'regular-candidate-parent-receipt')
        raw=source.read(1024*1024+1)
    ubuntu.require(len(raw)<=1024*1024,'bounded-candidate-parent-receipt')
    return raw,ubuntu.parse(raw)


def source_images(build,runtime=RUNTIME):
    files=build['runtime_files'];selected={}
    for name in ['bin/ffmpeg','bin/ffprobe']:
        ubuntu.require(name in files,'complete-custom-media-programs');selected[runtime/name]=dict(sha256=files[name],source_component='ffmpeg')
    for family in ['libavcodec','libavformat','libavutil','libavfilter','libswscale','libx264']:
        names=[name for name in files if re.fullmatch('lib/'+family+r'\.so\.[0-9]+(?:\.[0-9]+)*',name)]
        ubuntu.require(len(names)==1,'one-exact-custom-media-library')
        name=names[0];selected[runtime/name]=dict(sha256=files[name],source_component='x264' if family=='libx264' else 'ffmpeg')
    ubuntu.require(all(re.fullmatch('[0-9a-f]{64}',v['sha256']) for v in selected.values()),'custom-media-image-hashes')
    return selected


def refresh_evidence(baseline_path,refresh_path,head):
    braw,b=read(baseline_path);rraw,r=read(refresh_path);expected=ubuntu.source_inputs()
    ubuntu.require(b['scope']=='hosted-ubuntu-native-gateway-startup-images-and-system-python-tls'
        and b['schema']==1 and b['advisory_status']=='complete' and type(b['qualified']) is bool,
        'complete-stock-refresh-baseline')
    ubuntu.require(r['scope']=='hosted-native-package-refresh-not-security-admission' and r['schema']==1
        and r['qualified'] is False and r['package_refresh_complete'] is True,'executed-distribution-refresh')
    ubuntu.require(all(v['checkout']==dict(clean=True,commit=head) and v['inputs']==expected for v in [b,r]),'same-current-refresh-checkout')
    ubuntu.require(r['baseline_sha256']==hashlib.sha256(braw).hexdigest() and r['packages_before']==b['inventory']['packages']
        and set(r['packages_before'])==set(r['packages_after']),'same-complete-distribution-refresh')
    ubuntu.require(r['changed_packages']==[n for n in sorted(r['packages_before']) if r['packages_before'][n]!=r['packages_after'][n]],'exact-refresh-changes')
    return dict(baseline_sha256=hashlib.sha256(braw).hexdigest(),refresh_sha256=hashlib.sha256(rraw).hexdigest()),r


def require_refreshed(actual,refresh):
    packages=actual['packages']
    ubuntu.require(packages and set(packages)<=set(refresh['packages_after'])
        and all(value==refresh['packages_after'][name] for name,value in packages.items()),'actual-candidate-packages-match-refreshed-origins')


def resources():
    return {**{k:(RUNTIME/'bin'/k if k in ['ffmpeg','ffprobe'] else proxy.RUNTIME/'bin/nginx' if k=='nginx' else Path(v)).resolve() for k,v in ubuntu.SYSTEM_ROOTS.items()},
            **{k:(ROOT/'gateway/target/debug'/v).resolve() for k,v in ubuntu.REPOSITORY_ROOTS.items()}}


def runtime_inventory():
    selected=resources()
    proof=media.verify(ROOT,RUNTIME,ffmpeg=selected['ffmpeg'],publisher=selected['publisher'],reader=selected['reader'])
    _,build=media.read(RUNTIME/'build.json');_,qualified=media.read(RUNTIME/'qualification.json')
    proxy_proof=proxy.verify(ROOT,ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip(),source_inputs())
    origins=source_images(build);origins[selected['nginx']]=dict(sha256=proxy_proof['binary_sha256'],source_component='nginx');actual=ubuntu.inventory(selected,origins)
    ubuntu.require({p.as_posix():ubuntu.file_digest(p) for p in ubuntu.dependencies(selected['nginx'])}==proxy_proof['libraries'],'same-executed-proxy-libraries')
    ubuntu.require({name for name,value in actual['files'].items() if value.get('origin')=='pinned-source'}=={p.as_posix() for p in origins},'complete-actual-custom-images')
    for name in ['publisher','reader']:
        expected={str(RUNTIME/'lib'/n):h for n,h in qualified['sdk'][name]['libraries'].items()}
        observed={n:actual['files'][n]['sha256'] for n in actual['roots'][name]['libraries']
                  if actual['files'][n].get('origin')=='pinned-source'}
        ubuntu.require(expected==observed and actual['roots'][name]['sha256']==proof['sdk'][name],'same-executed-custom-sdk-libraries')
    return proof,build,proxy_proof,actual


def run(report,baseline,refresh):
    ubuntu.hosted() # Before files, native tools, runtime evidence or network.
    ubuntu.require(not os.path.lexists(report),'new-candidate-native-report')
    head=ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    ubuntu.require(not ubuntu.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-candidate-native-source')
    before=source_inputs();parent,refreshed=refresh_evidence(baseline,refresh,head)
    # Warm the actual system-Python TLS/threaded query path before its image snapshot.
    control,_,_=ubuntu.query({'ffmpeg':'7:6.1.1-3ubuntu5'})
    proof,build,proxy_proof,actual=runtime_inventory();require_refreshed(actual,refreshed)
    ubuntu.require(proof['checkout']==dict(clean=True,commit=head),'same-qualified-candidate-checkout')
    value=dict(schema=1,scope=SCOPE,qualified=False,advisory_status='pending',checkout=dict(commit=head,clean=True),
        inputs=before,inventory=actual,media_runtime=proof,proxy_runtime=proxy_proof,source_components=build['sources'],x264_cleanup=build['x264_cleanup'],
        custom_source_advisory_status='not-assessed-by-ubuntu-package-ranges',
        parent=parent,refreshed_distribution_packages_verified=True,ecosystem=ubuntu.ECOSYSTEM,advisory_api=ubuntu.API,
        advisory_responses_sha256=[],findings=None,withdrawn_records=[],
        negative_control=dict(package='ffmpeg',version='7:6.1.1-3ubuntu5',installed_for_control=False,findings=control),
        checked_on=datetime.now(timezone.utc).isoformat(),package_payload_authenticated=False,dynamic_plugin_coverage=False,
        kernel_qualified=False,firmware_qualified=False,upstream_advisory_closure=False,full_native_advisory_closure=False,
        physical_qualified=False,fips_validated=False,deployment_permitted=False)
    identity=ubuntu.save_report(report,value)
    try:
        findings,receipts,withdrawn=ubuntu.query(actual['source_packages'])
        ubuntu.require(any(v['advisory']=='UBUNTU-CVE-2025-59734' for v in control),'actual-affected-ubuntu-control')
        after_proof,after_build,after_proxy,after_actual=runtime_inventory()
        ubuntu.require(before==source_inputs() and proof==after_proof and build==after_build and proxy_proof==after_proxy and actual==after_actual
            and parent==refresh_evidence(baseline,refresh,head)[0]
            and head==ubuntu.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
            and not ubuntu.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-candidate-native-inputs')
        value.update(qualified=not findings,advisory_status='complete',findings=findings,withdrawn_records=withdrawn,
                     advisory_responses_sha256=receipts)
    except Exception as error:
        category=str(error) if isinstance(error,ubuntu.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        value.update(qualified=False,advisory_status='failed',failure_category=category);ubuntu.save_report(report,value,identity);raise
    ubuntu.save_report(report,value,identity)
    return not findings


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['report','baseline','refresh']:p.add_argument('--'+name,type=Path,required=True)
    try:accepted=run(**vars(p.parse_args()))
    except Exception as error:
        category=str(error) if isinstance(error,ubuntu.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        p.exit(1,'Minimized native candidate assessment refused ('+category+').\n')
    if not accepted:p.exit(1,'Actual minimized candidate Ubuntu package advisories remain unresolved; public report retained.\n')
    print('PASS: actual minimized candidate Ubuntu package advisory scope; custom-source/release gates remain open')


if __name__=='__main__':main()
