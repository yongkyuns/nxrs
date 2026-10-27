"""Scoped hosted evidence for the actual system-Python Expat candidate."""
import hashlib,json,os,re,stat
from pathlib import Path

RUNTIME=Path('/usr/lib/nxrs-gateway-system-xml-qualification')
LINK=Path('/usr/lib/x86_64-linux-gnu/libexpat.so.1')
SCOPE='hosted-signed-expat-for-system-python'
POLICY='security/gateway-system-xml-dependencies.json'
CERTIFICATE='security/keys/expat-maintainer.asc'
INPUTS=('tools/native-packages/expat.py','gateway/tools/system_xml_evidence.py',POLICY,CERTIFICATE,
        'tests/host/test_system_xml.py','tests/host/native/system_xml.c','tests/host/native/system_xml.py',
        '.github/workflows/gateway-system-xml.yml','tools/ci-apt.py',
        'tools/gateway-proxy/qualify.py','tools/gateway-proxy/advisories.py','tests/host/test_gateway_proxy.py',
        'tools/native-packages/payloads.py','tools/native-packages/ubuntu.py','gateway/fuzz/run.py',
        'gateway/tests/failure_location.py','tools/native-packages/candidate.py',
        'tools/native-packages/pcre.py','gateway/tools/system_regex_evidence.py',
        'gateway/tools/gateway_media_evidence.py','gateway/tools/gateway_proxy_evidence.py',
        'gateway/tools/python_environment.py')
FLAGS=('full_native_advisory_closure','upstream_advisory_closure','upstream_advisory_gate_qualified','bootstrap_environment_qualified',
       'signature_key_revocation_lifecycle_qualified','installed_ubuntu_package_remediated',
       'whole_os_replacement_qualified','already_running_processes_requalified',
       'gateway_custom_python_requalified','whole_candidate_native_graph_qualified',
       'kernel_qualified','firmware_qualified','physical_qualified','fips_validated','deployment_permitted')
REQUIRED={'XML_ParserCreate','XML_ParserCreateNS','XML_ParserFree','XML_Parse','XML_ParseBuffer',
          'XML_GetBuffer','XML_SetUserData','XML_SetElementHandler','XML_SetCharacterDataHandler','XML_ExpatVersionInfo'}
FIELDS={'schema','scope','qualified','verification_status','checkout','inputs','source','source_signature',
        'vendor','soname','library','library_sha256','old_exports','new_exports','configuration_sha256',
        'upstream_tests','published_fixes','unresolved_published_advisories','upstream_reports','python','private_probe','selected_probe',
        'wrong_image_refusals','rollback_control_executed',*FLAGS}

def require(value,category):
    if not value:raise ValueError(category)

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def inputs(root):return {name:digest(root/name) for name in INPUTS}

def parse(raw):
    def unique(rows):
        result={}
        for name,value in rows:
            require(name not in result,'duplicate-system-xml-field');result[name]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)

def private_read(path,maximum):
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        a=os.fstat(source.fileno())
        require(stat.S_ISREG(a.st_mode) and a.st_uid==0 and a.st_mode&0o022==0 and a.st_nlink==1 and 0<a.st_size<=maximum,'protected-system-xml-file')
        raw=source.read(maximum+1);b=os.fstat(source.fileno())
        keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_gid','st_nlink')
        require(len(raw)==a.st_size and len(raw)<=maximum and all(getattr(a,k)==getattr(b,k) for k in keys),'unchanged-system-xml-read')
    return raw

def validate(value,root,head):
    require(isinstance(value,dict) and set(value)==FIELDS,'exact-system-xml-fields')
    require(type(value['schema']) is int and value['schema']==1 and value['scope']==SCOPE and value['qualified'] is True and value['verification_status']=='complete','complete-system-xml-scope')
    require(value['checkout']==dict(commit=head,clean=True) and value['inputs']==inputs(root),'same-system-xml-source')
    policy=parse((root/POLICY).read_bytes())
    require(value['source']==policy['source'] and value['source_signature']==dict(signer=policy['source']['key']['fingerprint'],sha256=policy['source']['sha256'],changed_archive_refused=True),'signed-system-xml-source')
    require(all(value[key] is False for key in FLAGS),'system-xml-scope-flags')
    require(value['soname']=='libexpat.so.1' and value['library']=='lib/libexpat.so.1.13.0','exact-system-xml-image')
    require(all(isinstance(value[k],str) and re.fullmatch('[a-f0-9]{64}',value[k]) for k in ('library_sha256','configuration_sha256')),'system-xml-image-digests')
    for key in ('old_exports','new_exports'):
        rows=value[key]
        require(isinstance(rows,list) and rows==sorted(set(rows)) and 1<=len(rows)<=512 and all(re.fullmatch('XML_[A-Za-z0-9_]+',v) for v in rows),'bounded-system-xml-exports')
    require(REQUIRED<=set(value['old_exports'])<=set(value['new_exports']),'preserved-system-xml-abi')
    baseline=policy['vendor_baseline'];vendor=value['vendor']
    require(set(vendor)=={'name','version','source','source_version','sha256','header_sha256','header_source_version'} and
            vendor['name']==baseline['binary_package'] and vendor['source']==baseline['source_package'] and
            vendor['version']==vendor['source_version']==vendor['header_source_version']==baseline['version'] and
            vendor['sha256']==baseline['sha256'] and re.fullmatch('[a-f0-9]{64}',vendor['header_sha256']),'bound-system-xml-vendor-baseline')
    tests=value['upstream_tests']
    require(set(tests)=={'total','passed','skipped','failed','errors','checks'} and
            {k:v for k,v in tests.items() if k!='checks'}==dict(total=1,passed=1,skipped=0,failed=0,errors=0) and
            type(tests['checks']) is int and 1<=tests['checks']<=100000,'actual-system-xml-upstream-suites')
    require(value['published_fixes']==policy['documented_fix_releases'] and
            value['unresolved_published_advisories']==policy['unresolved_published_advisories'] and
            not set(value['published_fixes'])&set(value['unresolved_published_advisories']) and
            set(value['published_fixes'])|set(value['unresolved_published_advisories'])==set(policy['matched_distribution_advisories']),'published-and-unresolved-system-xml-records')
    review=value['upstream_reports']
    require(set(review)=={'reference','state','updated_at','body_sha256','unchecked_items','unchecked_items_are_not_vulnerability_count'} and
            review['reference']==policy['upstream_issue']['reference'] and review['state'] in ('open','closed') and
            type(review['unchecked_items']) is int and 0<=review['unchecked_items']<=256 and
            review['unchecked_items_are_not_vulnerability_count'] is True and
            isinstance(review['updated_at'],str) and re.fullmatch('[0-9TZ:.-]{20,40}',review['updated_at']) and
            re.fullmatch('[a-f0-9]{64}',review['body_sha256']),'bounded-upstream-system-xml-report-status')
    py=value['python']
    require(set(py)=={'schema','version','exact_loaded_image','image_sha256','elementtree','fragmented_namespace_utf8','malformed_document_refused'} and
            type(py['schema']) is int and py['schema']==1 and py['version']=='expat_2.9.0' and
            py['image_sha256']==value['library_sha256'] and all(py[k] is True for k in ('exact_loaded_image','elementtree','fragmented_namespace_utf8','malformed_document_refused')),'actual-system-python-xml-consumer')
    require(value['private_probe'] is value['selected_probe'] is value['rollback_control_executed'] is True and
            type(value['wrong_image_refusals']) is int and value['wrong_image_refusals']==2,'actual-system-xml-linkage-controls')
    return value

def verify(root,head):
    raw=private_read(RUNTIME/'qualification.json',65536);value=parse(raw);validate(value,root,head)
    library=RUNTIME/value['library']
    require(LINK.is_symlink() and LINK.resolve(strict=True)==library and library.resolve(strict=True)==library,'selected-system-xml-library')
    require(hashlib.sha256(private_read(library,16*1024*1024)).hexdigest()==value['library_sha256'],'same-installed-system-xml-library')
    return dict(qualification_sha256=hashlib.sha256(raw).hexdigest(),library_sha256=value['library_sha256'])
