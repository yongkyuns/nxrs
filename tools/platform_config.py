"""Compatibility for qualification runners; Rust owns profile resolution and validation."""
from pathlib import Path
import json
import functools
import os
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _frontend():
    environment=dict(os.environ)
    for key in ('RUSTC','CARGO_BUILD_RUSTC','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS','CARGO_BUILD_TARGET'):
        environment.pop(key,None)
    result=subprocess.run(['cargo','+1.90.0','build','--quiet','--locked','-p','nxrs-build','--target-dir',str(ROOT/'target')],cwd=ROOT,env=environment,text=True,capture_output=True,timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return ROOT/'target/debug'/('nxrs-build.exe' if os.name=='nt' else 'nxrs-build')


def _query(action, data=None, root=ROOT, **arguments):
    result = subprocess.run([str(_frontend()),'inspect-profile'], cwd=ROOT,
                            input=json.dumps(dict(action=action, data=data, root=str(root), **arguments)),
                            text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return json.loads(result.stdout)


CAPABILITIES = set(_query('capabilities'))
HOST_OS = {'linux', 'macos', 'windows'}
MODEL_FEATURES = {'vww': 'person-model', 'pico': 'pico-model'}


def load(path):
    with Path(path).open('rb') as stream:
        return tomllib.load(stream)


def resolve(name, root=ROOT):
    return _query('resolve', root=root, name=name)


def validate_target(platform, target_os):
    _query('validate-target', platform, os=target_os)


def cargo_output(platform, capability, target_os, root=ROOT):
    return _query('cargo', platform, root=root, capability=capability, os=target_os)


def native_helpers(platform, root=ROOT):
    return [Path(path) for path in _query('native-helpers', platform, root=root)]


def model_features(platform, manifest, root=ROOT):
    return _query('model-features', platform, root=root, manifest=str(manifest))


def detector_config(platform):
    return _query('detector', platform)


def gnss_config(platform):
    return _query('gnss', platform)


def imu_config(platform):
    return _query('imu', platform)


def scheduling_config(platform):
    return _query('scheduling', platform)


def console_config(platform):
    return _query('console', platform)


def network_initialization(platform):
    return _query('network', platform)
