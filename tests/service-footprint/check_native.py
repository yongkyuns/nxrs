#!/usr/bin/env python3
"""Test the experimental pthread adapter and release gate without a device."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    here = Path(__file__).resolve().parent
    root = here.parents[1]
    compiler = shutil.which(os.environ.get('CC', 'cc'))
    if compiler is None:
        raise SystemExit('a host C compiler is required')
    with tempfile.TemporaryDirectory(prefix='nxrs-footprint-native-') as directory:
        output = Path(directory)
        objects = []
        for name in ('native_thread', 'transport_gate', 'transport_gate_host_clock'):
            target = output / f'{name}.o'
            subprocess.run([compiler, '-std=c11', '-O2', '-pthread', '-c',
                            str(here / f'{name}.c'), '-o', str(target)], check=True)
            objects.append(str(target))
        subprocess.run(['ar', 'rcs', str(output / 'libtransport_host.a'), *objects], check=True)
        env = dict(os.environ)
        env['RUSTFLAGS'] = (env.get('RUSTFLAGS', '') +
                            f' -Lnative={output} -lstatic=transport_host').strip()
        features = ('packet-inplace-samples,synchronized-scale,ffi-scale-entry,'
                    'native-scale-worker,shared-mq-code')
        subprocess.run(['rustup', 'run', '1.90.0', 'cargo', 'test', '--locked',
                        '-p', 'nxrs-footprint-demo', '--bin', 'cq-scale',
                        '--features', features], cwd=root, env=env, check=True)


if __name__ == '__main__':
    main()
