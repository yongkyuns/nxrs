#!/usr/bin/env python3
"""Qualify profile-derived model dependencies and generated stream wiring on a host.

No camera, radio or NuttX ABI calls execute. The temporary product profile uses
the real host binding and each board product's model and worker budget.
"""
from pathlib import Path
import os
import json
import tempfile
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from platform_config import detector_config, resolve, validate_target


def main() -> None:
    host = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux')
    profile = ROOT / 'tests/profiles' / ('qualification-detector-' + uuid.uuid4().hex + '.toml')
    output = tempfile.TemporaryDirectory(prefix="nxrs-detector-composition-")
    try:
        for suffix, expected in [('', ['nxrs-detector/pico-model']),
                                 ('-vww', ['nxrs-detector/person-model']), ('-video', [])]:
            config = detector_config(resolve('esp32s3-camera-stream' + suffix))
            text = f'extends="{host}"\n'
            if config:
                text += (f'[detector]\nmodel="{config["model"]}"\n'
                         f'stack-bytes={config["stack-bytes"]}\nsample-ms={config["sample-ms"]}\n')
            else:
                text += 'detector={}\n'
            text += '[hal]\ncamera="mock"\n'
            if not config:
                text += 'detector="none"\n'
            profile.write_text(text)
            product = resolve(profile.stem)
            validate_target(product, host)
            for probe in (False, True):
                # Model injection is qualified through the real build invocation.
                # SDK-free profile inspection deliberately leaves it unresolved.
                build = ['cargo', 'nxrs', 'build', '--manifest-path',
                         'tests/nuttx-std/stream/Cargo.toml', '--platform', host,
                         '--profile', profile.stem, '--out', output.name]
                if probe:
                    build += ['--features', 'probe']
                subprocess.run(build, cwd=ROOT, check=True, timeout=600)
                evidence = json.loads((Path(output.name) / 'provider-selection.json').read_text())
                assert evidence['dependency_resolution']['status'] == 'resolved', evidence
                features = evidence['model_features']
                assert features == expected, (features, expected)
                selected = [*features, *(['nxrs-std-stream/probe'] if probe else [])]
                command = ['cargo', 'test', '--locked', '-p', 'nxrs-detector', '-p', 'nxrs-std-stream']
                if selected:
                    command += ['--features', ','.join(selected)]
                print(f'Qualify {config or "video-only"}, probe={probe}', flush=True)
                subprocess.run(command, cwd=ROOT, env={**os.environ, 'NXRS_PLATFORM': profile.stem},
                               check=True, timeout=240)
    finally:
        profile.unlink(missing_ok=True)
        output.cleanup()
    print('PASS: six profile-derived product compositions; real inference and generated probe modules')


if __name__ == '__main__':
    main()
