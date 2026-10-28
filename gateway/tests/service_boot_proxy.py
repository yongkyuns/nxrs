"""Re-exercise the signed proxy against the exact generated guest ABI.

The parent signature/build proof is retained by digest. Its host-library
qualification is never relabelled as a qualification of the guest libraries.
Execution admission belongs to the exact guest context, before this fixture.
"""
from datetime import datetime,timezone
import hashlib,importlib.util,json,os,re,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tools'))
import stage_proxy
import gateway_proxy_evidence as evidence

SCOPE='generated-service-boot-proxy-abi'
BOUNDARY=dict(tests=4,failures=0,skips=0,log_discard_qualified=True,log_configuration_refusals=2)

def require(value,category):evidence.require(value,category)
def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()

def complete_boundaries(value):
    return (isinstance(value,dict) and value==BOUNDARY and
            all(type(value[name]) is int for name in ('tests','failures','skips','log_configuration_refusals')) and
            value['log_discard_qualified'] is True)

def approval(parent_raw,review_raw,binary,libraries,boundary,head,inputs):
    """Bind new test evidence to verified parent bytes and actual guest images."""
    parent=json.loads(parent_raw);review=json.loads(review_raw)
    pin=json.loads((ROOT/'security/gateway-proxy-dependencies.json').read_bytes())
    require(type(parent['schema']) is int and parent['schema']==1 and parent['scope']==evidence.SCOPE and parent['qualified'] is True and
            parent['verification_status']=='complete' and parent['checkout']==dict(clean=True,commit=head) and
            parent['inputs']==inputs and parent['sources']==pin['sources'],'same-signed-proxy-parent')
    parent_hash=hashlib.sha256(parent_raw).hexdigest();review_hash=hashlib.sha256(review_raw).hexdigest()
    policy_hash=evidence.digest(ROOT/'security/gateway-proxy-dependencies.json')
    require(type(review['schema']) is int and review['schema']==1 and review['scope']=='operator-approved-minimal-camera-proxy' and
            review['qualification_sha256']==parent_hash and review['source_policy_sha256']==policy_hash and
            review['binary_sha256']==parent['binary_sha256']==binary,'same-parent-proxy-review')
    require(set(parent['libraries'])==set(stage_proxy.LIBRARIES.values()) and
            review['libraries']=={key:parent['libraries'][path] for key,path in stage_proxy.LIBRARIES.items()},
            'parent-proxy-library-binding')
    for name in ('nginx','pcre2'):
        signed=parent['source_signatures'][name];expected=pin['sources'][name]
        require(signed==dict(signer=expected['key']['fingerprint'],sha256=expected['sha256'],changed_archive_refused=True),
                'parent-proxy-signature-refusal')
    require(parent['changed_signature_refusals']==2 and parent['source_version_refusals']==2 and
            parent['pcre2_probe_passed'] is True and parent['pcre2_jit'] is False and
            parent['pcre2_version']==pin['sources']['pcre2']['version'],'parent-proxy-source-controls')
    require(complete_boundaries(boundary) and set(libraries)==set(stage_proxy.LIBRARIES) and
            all(isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) for v in libraries.values()),
            'actual-complete-guest-proxy-boundaries')
    window=pin['advisory_review'];start=int(datetime.fromisoformat(window['reviewed_at']).timestamp())
    end=int(datetime.fromisoformat(window['valid_until']).timestamp());now=int(datetime.now(timezone.utc).timestamp())
    require(review['reviewed_at']==start and review['valid_until']==end and start<=now<end and 0<end-start<=31*86400,
            'current-guest-proxy-review-window')
    receipt=dict(schema=1,scope=SCOPE,parent_qualification_sha256=parent_hash,parent_review_sha256=review_hash,
        binary_sha256=binary,libraries=libraries,boundaries=boundary,
        source_policy_sha256=policy_hash,guest_abi_qualified=True,whole_os_qualified=False,
        full_native_advisory_closure=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    updated=dict(review,qualification_sha256=hashlib.sha256(encoded(receipt)).hexdigest(),libraries=libraries)
    return receipt,updated

def execute(binary,report,parent_raw,review_raw,head,inputs,expected_libraries):
    """Called only after the guest's root/kernel/nonce/source admission succeeds."""
    require(set(expected_libraries)==set(stage_proxy.LIBRARIES),'declared-guest-proxy-libraries')
    def actual():return {name:evidence.digest(path) for name,path in stage_proxy.LIBRARIES.items()}
    before=actual();binary_hash=evidence.digest(binary)
    require(before==expected_libraries and binary_hash==json.loads(parent_raw)['binary_sha256'],
            'same-planned-guest-proxy-images')
    # The inherited test class discovers nginx at import time. Only this owned
    # binary's directory is prepended; no host environment is copied or forged.
    saved=os.environ.get('PATH');os.environ['PATH']=str(binary.parent)+':/usr/sbin:/usr/bin:/sbin:/bin'
    sys.path.insert(0,str(ROOT/'tools/gateway-proxy'))
    try:
        spec=importlib.util.spec_from_file_location('service_boot_https_boundary',ROOT/'tools/gateway-proxy/boundary.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        result=module.execute_boundaries(binary,report)
    finally:
        if saved is None:os.environ.pop('PATH',None)
        else:os.environ['PATH']=saved
    require(actual()==before and evidence.digest(binary)==binary_hash,'unchanged-executed-guest-proxy-images')
    return approval(parent_raw,review_raw,binary_hash,before,result,head,inputs)
