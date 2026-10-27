#!/usr/bin/env python3
"""Execute the shared real HTTPS boundaries against this exact owned proxy."""
import argparse
from contextlib import ExitStack
import importlib.util
from itertools import islice
import json
import os
from pathlib import Path
import stat
import sys
import unittest
import advisories

ROOT=advisories.ROOT
native=advisories.native


spec=importlib.util.spec_from_file_location('proxy_failure_locations',ROOT/'gateway/tests/failure_location.py')
locations=importlib.util.module_from_spec(spec);spec.loader.exec_module(locations)
PUBLIC_SOURCES={'tools/gateway-proxy/boundary.py','gateway/tests/test_vpn_deployment.py','gateway/deploy/vpn.py'}


def failure_context(error):
    # Reuse the bounded source-only contract; never messages, locals, arguments or logs.
    source={str(ROOT/name):name for name in PUBLIC_SOURCES}
    return locations.trace_locations(error,source)


def log_policy(text):
    native.require(text.count('access_log')==1 and text.count('error_log')==1 and text.count('    access_log off;')==1 and text.count('error_log /dev/null emerg;')==1
                   and text.count('ssl_session_tickets off;')==1,'qualified-proxy-log-policy')


def inspect_log_destinations(process):
    """Pin each live descriptor while inspecting it; bound snapshot churn."""
    null=os.stat('/dev/null')
    native.require(stat.S_ISCHR(null.st_mode),'actual-proxy-null-destination')
    folder=Path('/proc')/str(process.pid)/'fd'
    for _ in range(3):
        native.require(process.poll() is None,'live-owned-proxy')
        entries=list(islice(folder.iterdir(),257))
        native.require(len(entries)<=256,'bounded-proxy-fd-snapshot')
        changed=False;null_found=False
        with ExitStack() as handles:
            for path in entries:
                try:descriptor=os.open(path,os.O_PATH|os.O_CLOEXEC)
                except FileNotFoundError:
                    # A readiness/HTTPS socket may close after enumeration.
                    # Inspect all remaining handles, then require a new complete
                    # snapshot instead of accepting a partial observation.
                    changed=True;continue
                handles.callback(os.close,descriptor)
                info=os.fstat(descriptor)
                native.require(not stat.S_ISREG(info.st_mode),'no-proxy-regular-log-destination')
                null_found |= stat.S_ISCHR(info.st_mode) and info.st_rdev==null.st_rdev
            native.require(process.poll() is None,'live-owned-proxy')
            if not changed:
                native.require(null_found,'actual-proxy-null-destination')
                return
    native.require(False,'stable-proxy-fd-snapshot')


def execute_boundaries(nginx,report):
    # Callers own execution admission: main requires a disposable hosted runner;
    # the virtual-boot fixture proves its exact private guest before calling.
    # This shared fixture always executes the same four real HTTPS categories.
    native.require(nginx.is_absolute() and nginx.resolve()==nginx and nginx.is_file() and not os.path.lexists(report),'owned-proxy-boundary-inputs')
    spec=importlib.util.spec_from_file_location('proxy_https_boundaries',ROOT/'gateway/tests/test_vpn_deployment.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    observations=[];refusals=[];failures=[];categories=[]
    class PublicResult(unittest.TextTestResult):
        def record(self,err):
            failures.extend(failure_context(err[1].with_traceback(err[2])))
            known={'qualified-proxy-log-policy','no-proxy-regular-log-destination','actual-proxy-null-destination','actual-proxy-log-policy-refusal',
                   'live-owned-proxy','bounded-proxy-fd-snapshot','stable-proxy-fd-snapshot'}
            if isinstance(err[1],native.Refusal) and str(err[1]) in known:categories.append(str(err[1]))
        def addError(self,test,err):self.record(err);super().addError(test,err)
        def addFailure(self,test,err):self.record(err);super().addFailure(test,err)
    class OwnedProxyBoundaries(module.HTTPSBoundaryTests):
        nginx_binary=str(nginx)
        @classmethod
        def observe(cls):
            text=(cls.root/'nginx.conf').read_text();log_policy(text)
            inspect_log_destinations(cls.process);observations.append(True)
            return text
        @classmethod
        def setUpClass(cls):
            super().setUpClass();text=cls.observe()
            for old,new in [('error_log /dev/null emerg;','error_log /tmp/owned-fixture.log emerg;'),('    access_log off;','    access_log /tmp/owned-fixture.log;')]:
                try:log_policy(text.replace(old,new,1))
                except native.Refusal:refusals.append(True)
                else:raise native.Refusal('actual-proxy-log-policy-refusal')
        @classmethod
        def tearDownClass(cls):
            try:cls.observe()
            finally:super().tearDownClass()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(OwnedProxyBoundaries)
    result=unittest.TextTestRunner(verbosity=1,resultclass=PublicResult).run(suite)
    passed=result.testsRun==4 and result.wasSuccessful() and not result.skipped and len(observations)==2 and len(refusals)==2
    value=dict(tests=result.testsRun,failures=len(result.failures)+len(result.errors),skips=len(result.skipped),log_discard_qualified=passed,log_configuration_refusals=len(refusals))
    if not passed:value.update(failure_locations=locations.project_locations(failures[:8],PUBLIC_SOURCES),failure_categories=sorted(set(categories)))
    with report.open('x') as output:output.write(json.dumps(value,sort_keys=True)+'\n')
    native.require(passed,'complete-live-proxy-boundaries')
    return value


def main():
    native.hosted() # Before argument-dependent file reads, writes or native work.
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nginx',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    execute_boundaries(args.nginx,args.report)


if __name__=='__main__':
    try:main()
    except Exception:sys.exit('Owned proxy HTTPS boundaries refused; private output retained locally.')
