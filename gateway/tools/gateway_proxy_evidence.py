"""Admission contract for the executed minimal camera proxy, separate from deployment."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import stat
import python_environment

RUNTIME=Path('/usr/lib/nxrs-gateway-proxy-qualification')
SCOPE='hosted-signed-minimal-native-camera-proxy'
INPUTS=('tools/gateway-proxy/advisories.py','tools/gateway-proxy/qualify.py','tools/gateway-proxy/boundary.py','tools/gateway-proxy/README.md',
        'security/gateway-proxy-dependencies.json','gateway/tools/gateway_proxy_evidence.py','gateway/tools/stage_proxy.py',
        'gateway/proxy/Cargo.toml','gateway/proxy/src/main.rs','gateway/proxy/src/admission.rs','gateway/proxy/README.md',
        'gateway/deploy/README.md','gateway/deploy/strict-services.md','gateway/deploy/strict_vpn.py',
        'gateway/README.md','gateway/deploy/revocation.md','gateway/tests/ingest_reference.py',
        'gateway/tests/ingest_diagnostics.py','gateway/tests/test_ingest_diagnostics.py',
        'gateway/tests/sdk_media_evidence.py','gateway/tests/test_sdk_media_evidence.py',
        'gateway/tests/test_strict_deployment.py','gateway/tests/revocation_reference.py','gateway/tests/sandbox_probe.py',
        'gateway/tests/fetch_mediamtx.py','gateway/tests/backend_revocation_reference.py',
        'gateway/tests/activity_reference.py','.github/workflows/monitoring-gateway.yml',
        'gateway/tests/test_proxy_runtime_paths.py','gateway/control/tests/security.rs','gateway/tests/service_lifetime.py','gateway/tests/test_service_lifetime.py','gateway/deploy/ingest_service.py','gateway/tests/test_ingest_service.py',
        'gateway/tests/combined_reference.py','gateway/tests/combined_viewer.py','gateway/tests/rust_camera.py',
        'gateway/tests/build_camera_source.py','gateway/tests/camera_source.rs','gateway/tests/test_combined_reference.py',
        'gateway/tests/test_combined_health.py','gateway/tests/test_rust_camera.py',
        'hal/camera/src/api.rs','service/stream/src/lib.rs','service/stream/src/server.rs','service/stream/src/worker.rs',
        'tests/host/test_stage_proxy.py','gateway/tests/systemd_reference.py',
        'gateway/tests/proxy_diagnostics.py','gateway/tests/test_proxy_diagnostics.py','.github/workflows/monitoring-systemd.yml',
        'gateway/deploy/vpn_peer.py','gateway/tests/test_vpn_rotation.py','gateway/tests/vpn_reference.py',
        'gateway/tests/vpn_admission_contract.py','gateway/tests/test_vpn_admission_contract.py','gateway/private-file/tests/files.rs',
        'gateway/tests/activity_credentials.py',
        'gateway/tests/vpn_probe.py','gateway/tests/vpn_progress.py','gateway/tests/test_vpn_progress.py','gateway/tests/test_vpn_retirement.py',
        'gateway/tests/test_vpn_deployment.py','gateway/tests/failure_location.py','gateway/tests/test_failure_location.py','gateway/deploy/vpn.py','gateway/tools/init_reference.py',
        'tests/host/test_gateway_proxy_advisories.py','tests/host/test_gateway_proxy.py',
        'tests/host/native/proxy_pcre.c','.github/workflows/gateway-proxy.yml')
# The hosted service executes these client/build helpers as well as the proxy.
# Reuse their declared material sets rather than leaving an unbound fixture path.
INPUTS=tuple(sorted(set(INPUTS)|set(python_environment.INPUTS)|{
    'gateway/proxy/src/lib.rs','gateway/proxy/src/install.rs','gateway/proxy/src/bin/install.rs','gateway/tests/proxy_pair.py','gateway/tests/test_proxy_pair.py',
    'gateway/tests/proxy_pair_reference.py','gateway/tests/proxy_pair_gdb.py','tests/host/test_proxy_pair_reference.py','.github/workflows/gateway-proxy-pair.yml',
    'tools/native-packages/pcre.py','gateway/tools/system_regex_evidence.py','tests/host/test_system_regex.py','tests/host/native/system_regex.c',
    '.github/workflows/monitoring-vpn.yml','.github/workflows/monitoring-combined.yml','.github/workflows/encrypted-activity.yml',
    'gateway/tools/media_engine.py','gateway/tools/backup_engine.py','gateway/tools/verify_media_evidence.py',
    'gateway/deploy/dependencies.json','gateway/deploy/mediamtx/go.mod','gateway/deploy/mediamtx/go.sum'}))


def require(value,category):
    if not value:raise ValueError(category)


def digest(path):
    with open(path,'rb') as source:
        h=hashlib.sha256()
        while chunk:=source.read(1024*1024):h.update(chunk)
    return h.hexdigest()


def read(path):
    with open(path,'rb',opener=lambda p,f:os.open(p,f|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode),'regular-proxy-receipt');raw=source.read(1024*1024+1)
    require(len(raw)<=1024*1024,'bounded-proxy-receipt')
    def unique(rows):
        result={}
        for key,value in rows:require(key not in result,'duplicate-proxy-field');result[key]=value
        return result
    return raw,json.loads(raw,object_pairs_hook=unique)


def admit(value,root,head,inputs,runtime=RUNTIME):
    pin=json.loads((root/'security/gateway-proxy-dependencies.json').read_bytes())
    require(type(value['schema']) is int and value['schema']==1 and value['scope']==SCOPE and value['qualified'] is True and value['verification_status']=='complete','qualified-proxy-required')
    require(value['checkout']==dict(clean=True,commit=head) and value['inputs']==inputs,'same-current-proxy-source')
    require(value['sources']==pin['sources'],'exact-reviewed-proxy-sources')
    review=pin['advisory_review'];now=datetime.now(timezone.utc)
    begin=datetime.fromisoformat(review['reviewed_at']);end=datetime.fromisoformat(review['valid_until'])
    require(begin.tzinfo is not None and end.tzinfo is not None and begin<=now<end and 0<(end-begin).total_seconds()<=31*86400,'current-proxy-review-window')
    for name in ['nginx','pcre2']:
        actual=value['advisory_review'];expected=review[name]
        require(actual[name+'_articles']==len(expected) and actual[name+'_feed_sha256']==hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'complete-current-proxy-advisories')

    require(value['https_boundary_tests']==4 and value['https_boundary_failures']==0 and value['https_boundary_skips']==0,'executed-proxy-boundaries')
    require(value['pcre2_probe_passed'] is True and value['pcre2_jit'] is False and value['pcre2_version']==pin['sources']['pcre2']['version'],'actual-fixed-static-pcre')
    require(value['advisory_review']['nginx_unfixed']==['CVE-2009-4487'] and value['log_discard_qualified'] is True and value['log_configuration_refusals']==2,'retained-proxy-log-control')
    require(value['advisory_review']['pcre2_unfixed']==[] and value['source_version_refusals']==2,'reviewed-proxy-source-advisories')
    require(set(value['source_signatures'])=={'nginx','pcre2'} and value['changed_signature_refusals']==2,'actual-proxy-source-signatures')
    for name,signed in value['source_signatures'].items():
        require(signed['signer']==pin['sources'][name]['key']['fingerprint'] and signed['sha256']==pin['sources'][name]['sha256'] and signed['changed_archive_refused'] is True,'exact-publisher-signatures')
    require(all(re.fullmatch('[a-f0-9]{64}',value[k]) for k in ['binary_sha256','static_pcre_archive_sha256','configuration_sha256','link_makefile_sha256']),'actual-proxy-artifact-digests')
    binary=runtime/'bin/nginx';require(digest(binary)==value['binary_sha256'],'same-qualified-proxy-image')
    require(1<=len(value['libraries'])<=64,'nonempty-bounded-proxy-libraries')
    for path,sha in value['libraries'].items():
        require(isinstance(path,str) and re.fullmatch(r'/usr/[A-Za-z0-9_./+-]+',path) and PurePosixPath(path).as_posix()==path
                and '..' not in PurePosixPath(path).parts and re.fullmatch('[a-f0-9]{64}',sha),'canonical-proxy-library')
        require(digest(Path(path))==sha,'same-qualified-proxy-libraries')
    require(not any('pcre' in Path(n).name for n in value['libraries']),'no-unreviewed-dynamic-pcre')
    for flag in ['full_native_advisory_closure','bootstrap_environment_qualified','signature_key_revocation_lifecycle_qualified','physical_qualified','fips_validated','deployment_permitted']:
        require(value[flag] is False,'proxy-scope-limits')
    return dict(binary_sha256=value['binary_sha256'],libraries=value['libraries'],sources=value['sources'])


def verify(root,head,inputs,runtime=RUNTIME):
    raw,value=read(runtime/'qualification.json')
    return dict(qualification_sha256=hashlib.sha256(raw).hexdigest(),**admit(value,root,head,inputs,runtime))
