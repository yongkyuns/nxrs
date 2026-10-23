#!/usr/bin/env python3
"""Run the already-built hosted XCTest target in an explicitly test-owned iOS
simulator. Fixture trust is installed only in that disposable simulator."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


def main(path):
    device=os.environ['NXRS_IOS_SIMULATOR']
    products=Path(os.environ['NXRS_IOS_TEST_PRODUCTS']).resolve()
    def call(args,**kwargs):return subprocess.run(['xcrun','simctl']+args,check=True,timeout=120,**kwargs)
    devices=json.loads(subprocess.check_output(['xcrun','simctl','list','devices','available','--json']))
    match=[d for group in devices['devices'].values() for d in group if d['udid']==device]
    if len(match)!=1 or not match[0]['name'].startswith('Nxrs-multicamera-'):raise AssertionError('explicit test-owned simulator required')
    if match[0]['state']!='Booted':call(['boot',device],stdout=subprocess.DEVNULL)
    call(['bootstatus',device,'-b'],stdout=subprocess.DEVNULL)
    config=json.loads(path.read_text())
    call(['keychain',device,'add-root-cert',config['rootCertificate']],stdout=subprocess.DEVNULL)
    candidates=list(products.glob('CameraSessionTests_*.xctestrun'))
    if len(candidates)!=1:raise AssertionError('one built CameraSessionTests xctestrun required')
    value=plistlib.loads(candidates[0].read_bytes())
    found=0
    def configure(node):
        nonlocal found
        if isinstance(node,dict):
            if node.get('BlueprintName')=='NxrsMonitorSessionTests' or node.get('TestHostPath') and 'NxrsMonitorSessionTests' in node.get('TestBundlePath',''):
                node.setdefault('EnvironmentVariables',{})['NXRS_CLIENT_FIXTURE']=str(path)
                found+=1
            for child in node.values():configure(child)
        elif isinstance(node,list):
            for child in node:configure(child)
    configure(value)
    if found!=1:raise AssertionError('hosted test environment selection')
    # __TESTROOT__ is the directory containing the xctestrun file. Preserve
    # product references when creating a private, per-run configuration.
    def expand(node):
        if isinstance(node,str):return node.replace('__TESTROOT__',str(products))
        if isinstance(node,dict):return {key:expand(child) for key,child in node.items()}
        if isinstance(node,list):return [expand(child) for child in node]
        return node
    with tempfile.TemporaryDirectory(prefix='nxrs-ios-run-') as directory:
        root=Path(directory);root.chmod(0o700)
        run=root/'CameraSessionTests.xctestrun';run.write_bytes(plistlib.dumps(expand(value)))
        with (root/'private.log').open('wb') as log:
            result=subprocess.run(['xcodebuild','test-without-building','-xctestrun',str(run),'-destination',f'platform=iOS Simulator,id={device}',
                '-resultBundlePath',str(root/'results.xcresult'),'-parallel-testing-enabled','NO','-maximum-test-execution-time-allowance','120','-collect-test-diagnostics','never'],stdout=log,stderr=subprocess.STDOUT,timeout=180)
        text=(root/'private.log').read_text(errors='replace')
        markers=[line for line in text.splitlines() if line.startswith('MULTICAMERA IOS PASS:')]
        if result.returncode or len(markers)!=1:
            for line in text.splitlines():
                if 'CameraSessionTests.swift:' in line or 'error:' in line or 'TEST FAILED' in line:print(line.replace(config['token'],'<redacted>'),flush=True)
            raise AssertionError('iOS hosted multi-camera test failed')
        print(markers[0],flush=True)

if __name__=='__main__':
    try:main(Path(sys.argv[1]))
    except Exception as error:
        print('IOS QUALIFICATION FAILED:',str(error) if isinstance(error,AssertionError) else type(error).__name__,flush=True)
        raise SystemExit(1) from None
