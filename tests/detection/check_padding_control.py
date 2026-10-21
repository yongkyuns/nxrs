#!/usr/bin/env python3
"""Prove the actual Rust SAME-padding regression rejects an unpatched runtime."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix='nxrs-padding-control-') as directory:
        temporary = Path(directory)
        runtime = temporary / 'microflow'
        shutil.copytree(ROOT / 'vendor/microflow', runtime)
        subprocess.run(['git', 'apply', '--reverse', str(ROOT / 'upstream/microflow/patches/0001-tensorflow-same-padding.patch')],
                       cwd=runtime, check=True, capture_output=True)
        project = temporary / 'control'
        project.mkdir()
        (project / 'Cargo.toml').write_text(f'''[package]
name="nxrs-padding-negative-control"
version="0.1.0"
edition="2021"
[workspace]
[features]
person-model=[]
default=["person-model"]
[dependencies]
nxrs-detection-service={{path={json.dumps(str(ROOT / 'service/detection'))},features=["person-model"]}}
microflow="=0.1.3"
nalgebra={{version="=0.32.6",default-features=false,features=["macros"]}}
[patch.crates-io]
microflow={{path={json.dumps(str(runtime))}}}
[[test]]
name="inference"
path={json.dumps(str(ROOT / 'service/detection/tests/inference.rs'))}
''')
        # Retain the evaluated registry versions in this isolated workspace.
        shutil.copyfile(ROOT / 'Cargo.lock', project / 'Cargo.lock')
        environment = dict(os.environ, CARGO_TARGET_DIR=str(ROOT / 'target/detection-negative-control'))
        test = 'tensorflow_same_stride_two_even_input_pads_bottom_and_right'
        result = subprocess.run(['cargo', 'test', '--offline', '--manifest-path', str(project / 'Cargo.toml'),
                                 '--test', 'inference', test, '--', '--exact'], env=environment,
                                capture_output=True, text=True)
        log = ROOT / 'target/detection-negative-control.log'
        log.write_text(result.stdout + result.stderr)
        if result.returncode == 0 or f'test {test} ... FAILED' not in result.stdout:
            raise RuntimeError(f'negative control did not fail the expected runtime assertion; see {log}')
        print('PASS: removing the MicroFlow padding correction fails the actual Rust inference regression')


if __name__ == '__main__':
    main()
