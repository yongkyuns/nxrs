"""Public, bounded source/binary evidence shared by SDK media fixtures."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


def require(value, category):
    if not value: raise AssertionError(category)


def read_sdk(repository, path, camera):
    with Path(path).open('rb') as source: raw=source.read(65537)
    require(len(raw)<=65536, 'bounded public SDK evidence')
    report=json.loads(raw)
    require(report.get('schema')==1 and report.get('scope')=='actual-volatile-device-identity-host-sdk-and-issuer',
            'actual SDK qualification scope required')
    require(all(report.get(name) is True for name in
                ['qualified','device_tls_host_qualified','device_tls_capture_worker_host_qualified'])
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
    return raw,report


def source_names(repository, sdk):
    names=set(sdk['inputs'])
    for crate in ['ingest','control','private-file','activity']:
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
    counts=report.get('initial_viewer_frames')
    require(isinstance(counts,list) and len(counts)==2 and all(type(v) is int and v>=5 for v in counts),
            'two advancing decoded viewers required')
    for name in ['recovered_viewer_frames','source_connections','source_peak','source_frames','stopped_source_active']:
        require(type(report.get(name)) is int and 0<=report[name]<=2**63-1, 'bounded SDK media counters')
    require(report['recovered_viewer_frames']>=5 and report['source_connections']>=2
            and report['source_frames']>=10 and report['source_peak']==1 and report['stopped_source_active']==0,
            'complete SDK media recovery and retirement required')
