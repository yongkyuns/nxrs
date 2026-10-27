"""Bounded public evidence for a qualification-only shared PCRE2 replacement."""
from datetime import datetime,timezone
import hashlib,json,os
from pathlib import Path
import re,stat

RUNTIME=Path('/usr/lib/nxrs-gateway-system-regex-qualification')
LINK=Path('/usr/lib/x86_64-linux-gnu/libpcre2-8.so.0')
SCOPE='hosted-signed-shared-pcre2-for-system-tools'
INPUTS=('tools/native-packages/pcre.py','gateway/tools/system_regex_evidence.py',
        'tests/host/test_system_regex.py','tests/host/native/system_regex.c',
        'tools/gateway-proxy/qualify.py','tools/gateway-proxy/advisories.py',
        'security/gateway-proxy-dependencies.json','tools/native-packages/payloads.py',
        'tools/native-packages/ubuntu.py','gateway/fuzz/run.py','gateway/tests/failure_location.py')
FLAGS=('full_native_advisory_closure','bootstrap_environment_qualified',
       'signature_key_revocation_lifecycle_qualified','whole_os_replacement_qualified',
       'kernel_qualified','firmware_qualified','physical_qualified','fips_validated','deployment_permitted')
REQUIRED={'pcre2_config_8','pcre2_compile_8','pcre2_match_8','pcre2_code_free_8',
          'pcre2_jit_compile_8','pcre2_jit_match_8','pcre2_jit_stack_create_8','pcre2_jit_stack_assign_8'}

def require(value,category):
    if not value:raise ValueError(category)

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def inputs(root):return {name:digest(root/name) for name in INPUTS}
def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def private_read(path,maximum):
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_mode&0o022==0
                and before.st_nlink==1 and before.st_size<=maximum,'root-owned-system-regex-file')
        raw=source.read(maximum+1);after=os.fstat(source.fileno())
        require(len(raw)<=maximum and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'unchanged-system-regex-file')
    return raw

EXPORT=re.compile(r'(pcre2_[a-z0-9_]+_8)(?:(@@?)(PCRE2_[0-9]{2}\.[0-9]{2}))?')


def export_bases(rows):
    result=[]
    for token in rows:
        match=EXPORT.fullmatch(token)
        require(match is not None,'recognized-system-regex-export')
        result.append(match[1])
    return set(result)


def compatible_exports(old,new):
    old_bases=export_bases(old);export_bases(new)
    if not REQUIRED<=old_bases:return False
    selected=set(new)
    for token in old:
        match=EXPORT.fullmatch(token);base,marker,version=match.groups()
        if marker is None:
            defaults=[name for name in new if EXPORT.fullmatch(name)[1]==base
                      and EXPORT.fullmatch(name)[2] in (None,'@@')]
            if len(defaults)!=1:return False
        elif marker=='@@':
            if token not in selected:return False
        elif token not in selected and base+'@@'+version not in selected:return False
    return True


def validate(value,root,head):
    fields={'schema','scope','qualified','verification_status','checkout','inputs','source','source_signature','upstream_advisories',
            'affected_source_version_refused','upstream_tests','jit_enabled','loaded_library_probe_passed','old_exports','new_exports',
            'soname','library','library_sha256','old_library_sha256','configuration_sha256','version_script_sha256','consumer_names'}|set(FLAGS)
    require(set(value)==fields,'classified-system-regex-fields')
    policy=json.loads((root/'security/gateway-proxy-dependencies.json').read_bytes());pin=policy['sources']['pcre2']
    require(type(value['schema']) is int and value['schema']==1 and value['scope']==SCOPE
            and value['qualified'] is True and value['verification_status']=='complete','qualified-system-regex-required')
    require(value['checkout']==dict(commit=head,clean=True) and value['inputs']==inputs(root),'same-system-regex-source')
    require(value['source']==pin and value['source_signature']==dict(signer=pin['key']['fingerprint'],
            sha256=pin['sha256'],changed_archive_refused=True),'signed-system-regex-source')
    review=policy['advisory_review'];now=datetime.now(timezone.utc)
    begin=datetime.fromisoformat(review['reviewed_at']);end=datetime.fromisoformat(review['valid_until'])
    require(begin.tzinfo is not None and end.tzinfo is not None and begin<=now<end
            and 0<(end-begin).total_seconds()<=31*86400,'current-system-regex-review')
    def version(text):
        require(isinstance(text,str) and re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?',text),'system-regex-version')
        parts=tuple(map(int,text.split('.')));return parts+(0,)*(3-len(parts))
    require(all(version(pin['version'])>=version(row['fixed']) for row in review['pcre2'].values()),'fixed-system-regex-version')
    require(value['upstream_advisories']==dict(articles=len(review['pcre2']),
            feed_sha256=hashlib.sha256(encoded(review['pcre2'])).hexdigest(),unfixed=[])
            and value['affected_source_version_refused'] is True,'complete-system-regex-advisories')
    require(value['upstream_tests']==dict(total=4,passed=4,skipped=0,failed=0,errors=0)
            and value['jit_enabled'] is True and value['loaded_library_probe_passed'] is True,'executed-system-regex-controls')
    old=value['old_exports'];new=value['new_exports']
    require(all(isinstance(v,list) and v==sorted(set(v)) and 1<=len(v)<=512
                and all(EXPORT.fullmatch(name) for name in v) for v in [old,new])
            and compatible_exports(old,new),'system-regex-compatible-exports')
    abi=policy['shared_pcre2_abi']
    require(abi['upstream_version']==pin['version'] and abi['path']=='src/libpcre2-8.sym'
            and value['version_script_sha256']==abi['sha256'] and new==abi['default_exports']
            and all(EXPORT.fullmatch(name)[3] in abi['version_definitions'] for name in new),'reviewed-system-regex-version-script')
    require(value['soname']=='libpcre2-8.so.0' and re.fullmatch(r'lib/libpcre2-8\.so\.0\.[0-9]+\.[0-9]+',value['library']),
            'system-regex-library-identity')
    require(all(re.fullmatch('[a-f0-9]{64}',value[name]) for name in ['library_sha256','old_library_sha256','configuration_sha256']),
            'system-regex-artifact-digests')
    require(value['consumer_names']==['cryptsetup','service_manager'],'system-regex-consumer-linkage')
    require(all(value[flag] is False for flag in FLAGS),'system-regex-scope-limits')
    return value

def verify(root,head,runtime=RUNTIME,link=LINK):
    raw=private_read(runtime/'qualification.json',32768)
    def unique(rows):
        value={}
        for name,item in rows:require(name not in value,'duplicate-system-regex-field');value[name]=item
        return value
    value=validate(json.loads(raw,object_pairs_hook=unique),root,head)
    for directory in [runtime,runtime/'lib',link.parent]:
        metadata=directory.lstat();require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid==0
            and metadata.st_mode&0o022==0,'protected-system-regex-parent')
    library=runtime/value['library'];require(link.is_symlink() and link.lstat().st_uid==0 and link.readlink()==library,'selected-system-regex-library')
    require(hashlib.sha256(private_read(library,8*1024*1024)).hexdigest()==value['library_sha256'],'same-system-regex-library')
    return dict(qualification_sha256=hashlib.sha256(raw).hexdigest(),library_sha256=value['library_sha256'])


def current(root):
    import subprocess
    command=['git','-c','safe.directory='+str(root),'-C',str(root)]
    head=subprocess.check_output([*command,'rev-parse','HEAD'],stderr=subprocess.PIPE,text=True,timeout=30).strip()
    require(not subprocess.check_output([*command,'status','--porcelain'],stderr=subprocess.PIPE,timeout=30).strip(),
            'clean-system-regex-fixture-source')
    return verify(root,head)
