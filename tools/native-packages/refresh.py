#!/usr/bin/env python3
"""Refresh only the audited native package set on a disposable hosted image."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import ubuntu


def select(value,head,inputs):
    ubuntu.require(value['schema']==1 and value['scope']=='hosted-ubuntu-native-gateway-startup-images-and-system-python-tls'
                   and value['advisory_status']=='complete' and type(value['qualified']) is bool
                   and value['checkout']==dict(commit=head,clean=True) and value['inputs']==inputs,'current-complete-native-baseline')
    inventory=value['inventory'];packages=inventory['packages']
    ubuntu.require(isinstance(packages,dict) and 1<=len(packages)<=256,'refresh-package-budget')
    ubuntu.require(set(inventory['roots'])==set(ubuntu.SYSTEM_ROOTS)|set(ubuntu.REPOSITORY_ROOTS),'refresh-root-set')
    ubuntu.require({v['binary_package'] for v in inventory['files'].values()}==set(packages),'refresh-exact-image-owners')
    names=set()
    for identity,package in packages.items():
        name=ubuntu.package_name(package['name']);ubuntu.package_version(package['version']);ubuntu.package_version(package['source_version'])
        ubuntu.require(identity in [name,name+':amd64'] and package['architecture'] in ['amd64','all'],'refresh-native-package-identity')
        names.add(name)
    return sorted(names)


def run(baseline,report):
    ubuntu.hosted();ubuntu.require(not os.path.lexists(report),'new-refresh-report')
    with open(baseline,'rb') as source:raw=source.read(1024*1024+1)
    ubuntu.require(len(raw)<=1024*1024,'refresh-baseline-budget')
    value=ubuntu.parse(raw);inputs=ubuntu.source_inputs()
    head=ubuntu.command(['/usr/bin/git','-C',str(ubuntu.ROOT),'rev-parse','HEAD']).strip()
    ubuntu.require(not ubuntu.command(['/usr/bin/git','-C',str(ubuntu.ROOT),'status','--porcelain']).strip(),'clean-refresh-source')
    names=select(value,head,inputs);before=value['inventory']['packages']
    ubuntu.require(before=={identity:ubuntu.package(identity) for identity in before},'unchanged-refresh-baseline')
    spec=importlib.util.spec_from_file_location('native_ci_apt',ubuntu.ROOT/'tools/ci-apt.py')
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    # Reuse its signed, invocation-local official source list and existing
    # 64-package validation bound. Nothing runs on a personal host.
    for start in range(0,len(names),64):installer.install(names[start:start+64])
    after={identity:ubuntu.package(identity) for identity in before}
    ubuntu.require(inputs==ubuntu.source_inputs() and head==ubuntu.command(['/usr/bin/git','-C',str(ubuntu.ROOT),'rev-parse','HEAD']).strip(),'refresh-source-change')
    result=dict(schema=1,scope='hosted-native-package-refresh-not-security-admission',qualified=False,
                package_refresh_complete=True,baseline_sha256=ubuntu.digest(raw),checkout=dict(commit=head,clean=True),inputs=inputs,
                selected_package_names=names,packages_before=before,packages_after=after,
                changed_packages=[name for name in sorted(before) if before[name]!=after[name]],
                physical_qualified=False,fips_validated=False,full_native_advisory_closure=False,deployment_permitted=False)
    ubuntu.save_report(report,result)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--baseline',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    try:run(args.baseline,args.report)
    except Exception as error:
        category=str(error) if isinstance(error,ubuntu.Refusal) and __import__('re').fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Hosted native package refresh refused ('+category+').\n')
    print('PASS: audited native package set refreshed; a fresh advisory gate is still required')


if __name__=='__main__':main()
