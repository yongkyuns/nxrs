"""Public, bounded source/binary evidence shared by SDK media fixtures."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


def require(value, category):
    if not value: raise AssertionError(category)


def read_sdk(repository, path, camera):
    with Path(path).open('rb') as source: raw=source.read(65537)
    require(len(raw)<=65536, 'bounded public SDK evidence')
    report=json.loads(raw)
    require(report.get('schema')==1 and report.get('scope')=='actual-volatile-device-identity-host-sdk-and-issuer',
            'actual SDK qualification scope required')
    require(all(report.get(name) is True for name in
                ['qualified','device_tls_host_qualified','device_tls_capture_worker_host_qualified','device_tls_endpoint_lifecycle_host_qualified'])
            and all(report.get(name) is False for name in
                    ['physical_qualified','fips_validated','deployment_permitted']),
            'executed host SDK/capture qualification required')
    inputs=report.get('inputs')
    require(isinstance(inputs,dict) and 1<=len(inputs)<=128, 'bounded SDK source inventory')
    for name,digest in inputs.items():
        path=PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and path.parts
                and path.as_posix()==name and isinstance(digest,str)
                and re.fullmatch('[0-9a-f]{64}',digest), 'canonical SDK source identity')
        require(hashlib.sha256((repository/name).read_bytes()).hexdigest()==digest,
                'SDK source evidence changed')
    require(hashlib.sha256(Path(camera).read_bytes()).hexdigest()==report.get('device_tls_camera_fixture_sha256'),
            'endpoint binary must match the executed SDK report')
    endpoint=report.get('endpoint_controls',{})
    endpoint_counts=('exact_jpegs_verified','signed_updates_applied','malformed_forged_rollback_refusals',
                     'entropy_refreshes','revoked_peer_media_frames')
    require(all(type(endpoint.get(name)) is int and 0<=endpoint[name]<=2**63-1 for name in endpoint_counts),
            'bounded SDK endpoint lifecycle counts required')
    require(endpoint.get('scope')=='actual-host-sdk-endpoint-live-controls' and endpoint.get('qualified') is True
            and endpoint.get('camera_sha256')==report['device_tls_camera_fixture_sha256']
            and endpoint.get('exact_jpegs_verified',0)>=8 and endpoint.get('signed_updates_applied')==2
            and endpoint.get('malformed_forged_rollback_refusals')==3 and endpoint.get('entropy_refreshes')==1
            and endpoint.get('revoked_peer_media_frames')==0 and endpoint.get('idle_stream_retired') is True
            and endpoint.get('reconnect_refused') is True and endpoint.get('joined_cleanup') is True
            and all(endpoint.get(name) is False for name in ['physical_qualified','protected_sources_qualified',
                'device_key_rotation_qualified','deployment_permitted']), 'executed SDK endpoint lifecycle evidence required')
    return raw,report


def read_media(binary):
    # Actual executable and public sidecars must agree with the current recipe.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from verify_media_evidence import verify
    return verify(Path(binary))


def read_python():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from python_environment import installed
    return installed('media')


def source_names(repository, sdk):
    names=set(sdk['inputs'])
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from media_engine import INPUTS
    names.update(INPUTS)
    from gateway_media_evidence import INPUTS as runtime_inputs
    names.update(runtime_inputs)
    from python_environment import INPUTS as python_inputs
    names.update(python_inputs)
    for crate in ['ingest','control','private-file','activity','media-sandbox','media-process']:
        names.update(str(p.relative_to(repository)) for p in (repository/'gateway'/crate/'src').rglob('*') if p.is_file())
        names.add('gateway/'+crate+'/Cargo.toml')
        names.update(str(p.relative_to(repository)) for p in (repository/'gateway'/crate).glob('build.rs'))
    names.update(['gateway/Cargo.toml','gateway/Cargo.lock','gateway/tests/ingest_reference.py',
        'gateway/tests/revocation_reference.py','gateway/tests/ingest_diagnostics.py',
        'gateway/tests/test_ingest_diagnostics.py','gateway/tests/sdk_media_evidence.py',
        'gateway/tests/test_sdk_media_evidence.py','gateway/tests/media-requirements.txt',
        'tools/enrollment/requirements.txt','tools/enrollment/prepare_ingest.py',
        'gateway/deploy/dependencies.json','.github/workflows/monitoring-ingest.yml'])
    return names


def validate_media(report):
    require(report.get('schema')==1 and report.get('scope')=='actual-host-sdk-camera-gateway-media'
            and report.get('qualified') is True, 'complete SDK media receipt required')
    for name in ['sdk_endpoint_used','source_stall_reconnect_executed',
                 'credentials_absent_from_argv_environment','encoder_publisher_children_reaped']:
        require(report.get(name) is True, 'complete SDK media ownership controls required')
    for name in ['physical_qualified','fips_validated','deployment_permitted','device_tls_runtime_qualified',
                 'hardware_entropy_qualified','protected_device_key_storage_qualified',
                 'trusted_time_acquisition_qualified','protected_generation_qualified']:
        require(report.get(name) is False, 'host SDK media scope must remain explicit')
    if 'media_runtime' in report:
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from gateway_media_evidence import validate as validate_native
        try:validate_native(report['media_runtime'])
        except (ValueError,KeyError,TypeError):require(False,'complete scoped native media runtime proof required')
        require(report['media_runtime']['ffmpeg_sha256']==report.get('executables',{}).get('ffmpeg')
            and report['media_runtime']['sdk']['publisher']==report.get('executables',{}).get('publisher'),
            'same executed native media resources required')
    live=report.get('live_controls',{})
    require(live.get('malformed_forged_rollback_refusals')==3 and live.get('entropy_refreshes')==1
            and live.get('signed_update_reconnect_executed') is True
            and live.get('signed_peer_revocation_stops_media') is True
            and type(live.get('revocation_viewer_frames')) is int and live['revocation_viewer_frames']>=5
            and type(live.get('revoked_peer_media_frames')) is int and live['revoked_peer_media_frames']==0,
            'complete live SDK media controls required')
    counts=report.get('initial_viewer_frames')
    require(isinstance(counts,list) and len(counts)==2 and all(type(v) is int and v>=5 for v in counts),
            'two advancing decoded viewers required')
    for name in ['recovered_viewer_frames','source_connections','source_peak','source_frames','stopped_source_active']:
        require(type(report.get(name)) is int and 0<=report[name]<=2**63-1, 'bounded SDK media counters')
    require(report['recovered_viewer_frames']>=5 and report['source_connections']>=2
            and report['source_frames']>=10 and report['source_peak']==1 and report['stopped_source_active']==0,
            'complete SDK media recovery and retirement required')


FAILURE_REASONS = {
    'FileNotFoundError':'missing-file', 'PermissionError':'permission', 'ModuleNotFoundError':'missing-module',
    'ImportError':'module-import', 'ValueError':'invalid-value', 'TypeError':'invalid-type',
    'KeyError':'missing-field', 'OSError':'io',
    'actual TLS-ingested frames produce a new motion clip with observed child metadata':'motion-clip',
    'integrated recorder remains running':'recorder-exit',
    'integrated recorder retires before backup maintenance':'recorder-shutdown',
    'actual composed H264 recording fully decodes':'recording-decoding',
    'SDK source evidence changed':'sdk-source-changed',
    'endpoint binary must match the executed SDK report':'sdk-binary-changed',
    'SDK media receipt belongs to owned encrypted volume':'media-receipt-location',
    'actual Rust camera output encoded and published':'publication-start',
    'SDK-generated CSR readiness':'sdk-csr-readiness',
    'actual SDK TLS endpoint readiness':'sdk-tls-readiness',
    'bounded Rust fixture response':'sdk-response',
}


def public_failure(raw):
    # Parse only exact fixed phase/category lines from a bounded private result.
    # Unknown strings, paths, exception text and native logs never leave here.
    phases={'preflight','execution','receipt'}
    result=dict(schema=1,scope='bounded-sdk-recording-failure-category-only',qualified=False,
                phase='unknown',reason='unclassified')
    for line in raw[:65536].decode('ascii',errors='replace').splitlines():
        if line.startswith('SDK_MEDIA_PHASE '):
            value=line[len('SDK_MEDIA_PHASE '):]
            if value in phases: result['phase']=value
        if line.startswith('SDK_RECORDING_OBSERVATIONS ') and len(line)<=2048:
            from activity_diagnostics import recording_observations
            try: value=json.loads(line[len('SDK_RECORDING_OBSERVATIONS '):])
            except (ValueError,RecursionError): continue
            result['recording']=recording_observations(value)
        if line.startswith('INGEST REFERENCE FAILED: '):
            value=line[len('INGEST REFERENCE FAILED: '):]
            if value in FAILURE_REASONS: result['reason']=FAILURE_REASONS[value]
    return result
