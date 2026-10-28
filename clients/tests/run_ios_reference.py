#!/usr/bin/env python3
"""Run the already-built hosted XCTest target in an explicitly test-owned iOS
simulator. Fixture trust is installed only in that disposable simulator."""
import json
import importlib.util
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ios_fixture_process',ROOT/'gateway/fuzz/run.py')
process=importlib.util.module_from_spec(spec);spec.loader.exec_module(process)
SUCCESS='MULTICAMERA IOS PASS: two native decoders; independent epochs/pause; full-screen reuse; JPEG/clip capture; third-camera admission; background retirement; removed-camera cleanup (two consecutive cycles)'
CASES=frozenset(('testCatalogRemovalSelectsRemainingCameraAndClosesObsoleteFullscreen',
    'testCatalogAdditionKeepsSelectedCameraSession',
    'testManualPlaybackPreferenceStartsOnlyExplicitlyResumedCameras',
    'testIndependentNativeCamerasAndCaptureRetirement'))
URL_ADMISSION_CODES=frozenset((-999,*range(-1022,-999),*range(-1103,-1099),*range(-1206,-1199),-2000))


def diagnostics(raw,execution):
    """Project fixed test identities/coordinates and allowlisted NSError pairs."""
    if execution not in ('failed','refused'):raise ValueError('fixed execution category')
    if not isinstance(raw,bytes) or len(raw)>1024*1024:raise ValueError('bounded private test output')
    tests=[];locations=[];retirement=[];admission=[]
    for line in raw.decode(errors='replace').splitlines():
        case=re.fullmatch(r"Test Case '-\[(?:[A-Za-z0-9_]+\.)?CameraSessionTests (test[A-Za-z0-9_]+)\]' (started|passed|failed)(?:[ .(].*)?",line)
        if case and case[1] in CASES:
            row=dict(case=case[1],event=case[2])
            if row not in tests:tests.append(row)
        location=re.search(r'CameraSessionTests\.swift:([0-9]{1,5})(?::[0-9]{1,5})?: error:',line)
        if location and 1<=int(location[1])<=10000:
            row=dict(source='clients/ios/NxrsMonitorSessionTests/CameraSessionTests.swift',line=int(location[1]))
            if row not in locations and len(locations)<8:locations.append(row)
        # The owned XCTest gate prints this pair. Keep only fixed domains and
        # admitted integer codes; no private message, URL, response or SDK text.
        failure=re.search(r'Fixture HTTPS admission failed: (NSURLErrorDomain|NxrsMonitorCore\.MonitorError) (-?[0-9]{1,5})(?![0-9])',line)
        if failure:
            domain,code=failure[1],int(failure[2])
            allowed=(domain=='NSURLErrorDomain' and code in URL_ADMISSION_CODES
                     or domain=='NxrsMonitorCore.MonitorError' and 0<=code<=31)
            row=dict(domain=domain,code=code)
            if allowed and row not in admission and len(admission)<2:admission.append(row)
        if re.fullmatch(r'CAMERA DIAGNOSTIC: IOS RETIREMENT STATE: sessions=[0-9]{1,2} a=[01],[01],[01],[01],[01],[0-9]{1,20} b=[01],[01],[01],[01],[01],[0-9]{1,20}',line):
            if line not in retirement and len(retirement)<2:retirement.append(line)
    return dict(execution=execution,tests=tests,locations=locations,retirement=retirement,https_admission=admission)


def run_tests(command,root,seconds=180):
    # The outer fixture cancels this runner with TERM before escalating. Raise
    # through execute() so its reserved process-group owner can retire children.
    # selectors treats InterruptedError as a retryable system-call interruption.
    # Use a distinct exception so cancellation reaches the owner's finally block.
    def interrupted(*_):raise RuntimeError('owned iOS runner interrupted')
    previous=signal.signal(signal.SIGTERM,interrupted)
    try:
        try:
            status,_=process.execute(command,root,'ios-session-tests',dict(os.environ),seconds,cwd=ROOT)
            execution='failed'
        except Exception:
            status=None;execution='refused'
    finally:signal.signal(signal.SIGTERM,previous)
    log=root/'ios-session-tests.log'
    raw=b''
    if log.exists():
        with log.open('rb') as stream:raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024:raise AssertionError('iOS hosted test output limit')
    markers=[line for line in raw.decode(errors='replace').splitlines() if line.startswith('MULTICAMERA IOS PASS:')]
    if status!=0 or markers!=[SUCCESS]:
        print('IOS QUALIFICATION DIAGNOSTICS: '+json.dumps(diagnostics(raw,execution),sort_keys=True),flush=True)
        raise AssertionError('iOS hosted multi-camera execution refused' if status is None else 'iOS hosted multi-camera test failed')
    return SUCCESS


def main(path):
    device=os.environ['NXRS_IOS_SIMULATOR']
    products=Path(os.environ['NXRS_IOS_TEST_PRODUCTS']).resolve()
    def call(args,**kwargs):return subprocess.run(['xcrun','simctl']+args,check=True,timeout=120,**kwargs)
    devices=json.loads(subprocess.check_output(['xcrun','simctl','list','devices','available','--json']))
    match=[d for group in devices['devices'].values() for d in group if d['udid']==device]
    if len(match)!=1 or not match[0]['name'].startswith('Nxrs-multicamera-'):raise AssertionError('explicit test-owned simulator required')
    print('IOS QUALIFICATION: wait for disposable simulator boot',flush=True)
    if match[0]['state']!='Booted':call(['boot',device],stdout=subprocess.DEVNULL)
    call(['bootstatus',device,'-b'],stdout=subprocess.DEVNULL)
    config=json.loads(path.read_text())
    print('IOS QUALIFICATION: install fixture-only simulator trust',flush=True)
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
        print('IOS QUALIFICATION: execute hosted camera-session tests',flush=True)
        marker=run_tests(['xcodebuild','test-without-building','-xctestrun',str(run),'-destination',f'platform=iOS Simulator,id={device}',
            '-resultBundlePath',str(root/'results.xcresult'),'-parallel-testing-enabled','NO','-maximum-test-execution-time-allowance','120','-collect-test-diagnostics','never'],root)
        print(marker,flush=True)

if __name__=='__main__':
    try:main(Path(sys.argv[1]))
    except Exception as error:
        print('IOS QUALIFICATION FAILED:',str(error) if isinstance(error,AssertionError) else type(error).__name__,flush=True)
        raise SystemExit(1) from None
