#!/usr/bin/env python3
"""Audit frozen test-package graphs or inventory the actual installed environment."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
CONFIG=ROOT/'gateway/tests/python-environment.json'
INPUTS=('gateway/tools/python_environment.py','gateway/tests/python-environment.json',
        'gateway/tests/requirements.txt','gateway/tests/media-requirements.txt',
        'tools/enrollment/requirements.txt','gateway/tests/test_python_environment.py',
        '.github/workflows/python-media-security.yml','gateway/tests/python_media_reference.py',
        'gateway/tools/native_python.py','gateway/tests/native-python.json',
        'gateway/tests/native-python.md','gateway/tests/test_native_python.py','gateway/fuzz/run.py',
        '.github/workflows/python-native-media.yml',
        *('.github/workflows/'+name+'.yml' for name in ['monitoring-combined','monitoring-revocation',
            'monitoring-gateway','monitoring-systemd','monitoring-vpn','monitoring-ingest','encrypted-activity']))


class Refusal(ValueError):
    pass


def require(value,category):
    if not value:raise Refusal(category)


def normal(name):
    require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}',name), 'package-name')
    return re.sub(r'[-_.]+','-',name.lower())


def parse(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            require(key not in result,'duplicate-json-key');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def bounded(path,limit=1024*1024):
    # Inputs are public repository policy; do not accept unbounded/device files.
    import stat
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode),'regular-public-input')
        raw=source.read(limit+1)
    require(len(raw)<=limit,'public-input-size');return raw


def digest(raw):return hashlib.sha256(raw).hexdigest()


def inputs():return {name:digest(bounded(ROOT/name)) for name in INPUTS}


def lock(group):
    require(group in ['core','media'],'package-group')
    config=parse(bounded(CONFIG));expected=config['groups'][group]
    require(config['schema']==1 and config['python']=='3.12' and config['scanner']=='2.10.1'
            and isinstance(expected,dict) and 1<=len(expected)<=64,'environment-policy')
    entry=ROOT/('gateway/tests/requirements.txt' if group=='core' else 'gateway/tests/media-requirements.txt')
    allowed={(ROOT/name).resolve() for name in ['gateway/tests/requirements.txt','gateway/tests/media-requirements.txt','tools/enrollment/requirements.txt']}
    selected={};hashes={};seen=set()
    def visit(path):
        path=path.resolve();require(path in allowed and path not in seen,'requirements-include')
        seen.add(path);raw=bounded(path).decode()
        for line in raw.replace('\\\n',' ').splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('-r '):visit(path.parent/line[3:]);continue
            match=re.fullmatch(r'([A-Za-z0-9._-]+)==([A-Za-z0-9.+!-]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)',line)
            require(match is not None,'complete-hashed-requirement')
            name=normal(match[1]);require(name not in selected,'duplicate-requirement')
            selected[name]=match[2];hashes[name]=re.findall(r'--hash=sha256:([0-9a-f]{64})',match[3])
    visit(entry)
    require(selected==expected and all(normal(n)==n for n in expected),'complete-package-graph')
    return config,expected,entry,hashes


def response(status,raw,expected,*,affected=False):
    value=parse(raw);items=value['dependencies']
    require(isinstance(items,list) and len(items)==len(expected),'audit-complete-inventory')
    actual={};identities=set()
    for item in items:
        require(isinstance(item,dict) and item.get('skip_reason') is None,'audit-skipped-package')
        name=normal(item['name']);require(name not in actual,'audit-duplicate-package')
        actual[name]=item['version'];vulns=item['vulns']
        require(isinstance(vulns,list) and len(vulns)<=128,'audit-findings-schema')
        for finding in vulns:
            require(isinstance(finding,dict) and isinstance(finding.get('id'),str)
                    and re.fullmatch(r'(?:GHSA-[a-z0-9-]+|PYSEC-[0-9-]+|CVE-[0-9-]+)',finding['id']), 'audit-advisory-identity')
            aliases=finding.get('aliases',[])
            require(isinstance(aliases,list) and len(aliases)<=32 and all(isinstance(v,str)
                    and re.fullmatch(r'(?:GHSA-[a-z0-9-]+|PYSEC-[0-9-]+|CVE-[0-9-]+)',v) for v in aliases),'audit-advisory-aliases')
            identities.add(finding['id']);identities.update(aliases)
    require(actual==expected,'audit-package-identity')
    require(status==(1 if affected else 0),'audit-process-status')
    require(bool(identities)==affected,'audit-findings')
    return sorted(identities)


def request(python,args,directory,timeout=180,*,module='pip_audit',refusal_token=None):
    env=dict(PATH='/usr/bin:/bin',HOME=str(directory),PYTHONNOUSERSITE='1',PIP_CONFIG_FILE=os.devnull,
             PIP_NO_INPUT='1',PIP_DISABLE_PIP_VERSION_CHECK='1',LANG='C')
    # Advisory requests are metadata-only. The pip control downloads a healthy
    # wheel to an owned directory with a deliberately incorrect hash.
    require(module in ['pip_audit','pip'],'audit-tool-module')
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        try:
            result=subprocess.run([str(python),'-I','-m',module,*args],cwd=directory,env=env,
                stdin=subprocess.DEVNULL,stdout=output,stderr=errors,timeout=timeout,check=False)
        except subprocess.TimeoutExpired:raise Refusal('audit-deadline') from None
        output.seek(0);raw=output.read(4*1024*1024+1);errors.seek(0);error_raw=errors.read(4*1024*1024+1)
    require(len(raw)<=4*1024*1024 and len(error_raw)<=4*1024*1024,'audit-output-limit')
    if refusal_token is not None:require(refusal_token in error_raw,'actual-wheel-hash-refusal')
    return result.returncode,raw


def audit(python):
    before=inputs();groups={}
    with tempfile.TemporaryDirectory(prefix='nxrs-python-audit-') as temporary:
        directory=Path(temporary);status,version=request(python,['--version'],directory,30)
        require(status==0 and version.strip()==b'pip-audit 2.10.1','scanner-version')
        for group in ['core','media']:
            config,expected,entry,hashes=lock(group)
            # The auditor's metadata-only collector does not recursively collect
            # requirement includes. Flatten only the validated complete graph,
            # preserving every exact version and reviewed wheel hash.
            flattened=directory/(group+'.txt')
            flattened.write_text(''.join(name+'=='+expected[name]+' '+
                ' '.join('--hash=sha256:'+value for value in hashes[name])+'\n' for name in sorted(expected)))
            args=['--disable-pip','--require-hashes','--strict','--format','json','--progress-spinner','off',
                  '--cache-dir',str(directory/'current-cache'),'-r',str(flattened)]
            status,raw=request(python,args,directory);response(status,raw,expected)
            groups[group]=dict(packages=len(expected),advisory_ids=[])
        negative=config['negative'];path=directory/'affected.txt';path.write_text(negative['name']+'=='+negative['version']+'\n')
        status,raw=request(python,['--disable-pip','--no-deps','--strict','--format','json','--progress-spinner','off',
                                 '--cache-dir',str(directory/'negative-cache'),'-r',str(path)],directory)
        identities=response(status,raw,{negative['name']:negative['version']},affected=True)
        require(negative['required_advisory'] in identities,'actual-affected-advisory')
        altered=directory/'altered-hash.txt'
        altered.write_text('jsonschema=='+config['groups']['core']['jsonschema']+' --hash=sha256:'+'0'*64+'\n')
        status,_=request(python,['download','--no-deps','--require-hashes','--only-binary=:all:',
            '--no-cache-dir','--index-url','https://pypi.org/simple','--dest',str(directory/'wheel-control'),
            '-r',str(altered)],directory,module='pip',
            refusal_token=b'THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE')
        require(status==1,'wheel-hash-refusal-status')
    require(before==inputs(),'audit-source-change')
    return dict(schema=1,scope='fresh-complete-python-package-advisories',qualified=True,inputs=before,
        scanner='pip-audit 2.10.1',service='https://pypi.org',groups=groups,altered_wheel_hash_refused=True,
        affected_control=dict(package=negative['name'],version=negative['version'],installed=False,advisory_ids=identities),
        finished_on=datetime.now(timezone.utc).isoformat(),native_advisory_closure=False,
        physical_qualified=False,fips_validated=False,deployment_permitted=False)


def installed(group):
    _,expected,_,_=lock(group)
    require(sys.implementation.name=='cpython' and sys.version_info[:2]==(3,12)
            and sys.platform in ['linux','darwin'],'qualified-python-runtime')
    actual={};bootstrap={};records={}
    for distribution in metadata.distributions():
        name=normal(distribution.metadata['Name'])
        require(name not in actual and name not in bootstrap,'installed-duplicate-package')
        if name in ['pip','setuptools','wheel']:bootstrap[name]=distribution.version;continue
        actual[name]=distribution.version
        record=distribution.read_text('RECORD')
        require(isinstance(record,str) and 0<len(record)<=2*1024*1024,'installed-wheel-record')
        records[name]=digest(record.encode())
    require(actual==expected,'installed-complete-package-graph')
    native=dict(python_ssl=ssl.OPENSSL_VERSION)
    if group=='media':
        import av
        from cryptography.hazmat.backends import default_backend
        native.update(pyav={k:list(v) for k,v in av.library_versions.items()},
                      cryptography_openssl=default_backend().openssl_version_text())
    value=dict(schema=1,scope='exact-pinned-installed-python-package-metadata',qualified=True,group=group,
        inputs=inputs(),packages=actual,installed_record_sha256=records,bootstrap_packages=bootstrap,
        python=sys.version.split()[0],platform=sys.platform,native_versions=native,
        native_advisory_closure=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    if group=='media':
        from native_python import runtime_evidence
        value['native_environment']=runtime_evidence()
    validate(value,group);return value


def validate(value,group):
    _,expected,_,_=lock(group)
    require(value['schema']==1 and value['scope']=='exact-pinned-installed-python-package-metadata'
            and value['qualified'] is True and value['group']==group and value['packages']==expected
            and value['inputs']==inputs() and value['platform'] in ['linux','darwin']
            and re.fullmatch(r'3\.12\.[0-9]+',value['python']), 'installed-receipt-identity')
    records=value['installed_record_sha256'];require(set(records)==set(expected)
            and all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) for v in records.values()),'installed-record-inventory')
    require(isinstance(value['bootstrap_packages'],dict) and set(value['bootstrap_packages'])<=set(['pip','setuptools','wheel'])
            and all(isinstance(v,str) and 0<len(v)<=100 for v in value['bootstrap_packages'].values()),'bootstrap-inventory')
    native=value['native_versions'];require(isinstance(native,dict) and isinstance(native.get('python_ssl'),str)
            and 0<len(native['python_ssl'])<=256,'native-version-observation')
    if group=='media':
        require(isinstance(native.get('cryptography_openssl'),str) and 0<len(native['cryptography_openssl'])<=256
                and isinstance(native.get('pyav'),dict) and {'libavutil','libavcodec','libavformat'}<=set(native['pyav'])
                and all(isinstance(k,str) and re.fullmatch('lib[a-z0-9]+',k) and isinstance(v,list) and len(v)==3
                        and all(type(n) is int and 0<=n<1000000 for n in v) for k,v in native['pyav'].items()),'native-media-version-observation')
    if group=='media':
        from native_python import validate_evidence
        validate_evidence(value['native_environment'])
    require(all(value[k] is False for k in ['native_advisory_closure','physical_qualified','fips_validated','deployment_permitted']),
            'installed-receipt-scope')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['audit','inventory'])
    parser.add_argument('--audit-python',type=Path);parser.add_argument('--group',choices=['core','media'],default='media')
    parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    try:
        require(not os.path.lexists(args.report),'new-public-report')
        if args.mode=='audit':
            require(args.audit_python is not None,'auditor-required');value=audit(args.audit_python.absolute())
        else:value=installed(args.group)
        raw=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode();require(len(raw)<=65536,'bounded-public-report')
        with open(args.report,'xb') as output:output.write(raw)
    except (Refusal,OSError,KeyError,ValueError,TypeError,ImportError):
        parser.exit(1,'Python environment/advisory qualification refused. No report admitted.\n')
    print('PASS: frozen Python package',args.mode,'qualification')

if __name__=='__main__':main()
