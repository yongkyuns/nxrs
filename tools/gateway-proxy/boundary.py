#!/usr/bin/env python3
"""Execute the shared real HTTPS boundaries against this exact owned proxy."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import unittest
import advisories

ROOT=advisories.ROOT
native=advisories.native


def log_policy(text):
    native.require(text.count('access_log')==1 and text.count('error_log')==1 and text.count('    access_log off;')==1 and text.count('error_log /dev/null emerg;')==1
                   and text.count('ssl_session_tickets off;')==1,'qualified-proxy-log-policy')


def main():
    native.hosted()
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--nginx',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    native.require(args.nginx.is_absolute() and args.nginx.resolve()==args.nginx and args.nginx.is_file() and not os.path.lexists(args.report),'owned-proxy-boundary-inputs')
    spec=importlib.util.spec_from_file_location('proxy_https_boundaries',ROOT/'gateway/tests/test_vpn_deployment.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    observations=[];refusals=[]
    class OwnedProxyBoundaries(module.HTTPSBoundaryTests):
        nginx_binary=str(args.nginx)
        @classmethod
        def observe(cls):
            text=(cls.root/'nginx.conf').read_text();log_policy(text)
            targets=[]
            for path in (Path('/proc')/str(cls.process.pid)/'fd').iterdir():
                # The live owned process must hold no regular log/output files.
                info=path.stat();native.require(not stat.S_ISREG(info.st_mode),'no-proxy-regular-log-destination');targets.append(os.readlink(path))
            native.require('/dev/null' in targets,'actual-proxy-null-destination');observations.append(True)
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
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    native.require(result.testsRun==4 and result.wasSuccessful() and not result.skipped and len(observations)==2 and len(refusals)==2,'complete-live-proxy-boundaries')
    value=dict(tests=result.testsRun,failures=len(result.failures)+len(result.errors),skips=len(result.skipped),log_discard_qualified=True,log_configuration_refusals=2)
    with args.report.open('x') as output:output.write(json.dumps(value,sort_keys=True)+'\n')


if __name__=='__main__':
    try:main()
    except Exception:sys.exit('Owned proxy HTTPS boundaries refused; private output retained locally.')
