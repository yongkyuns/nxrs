#!/usr/bin/env python3
"""Build a Linux-only test observer and confirm it detects an actual decoder open."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys


def build(output):
    if sys.platform!='linux': raise RuntimeError('Linux dynamic-loader witness required')
    output=output.resolve();output.mkdir(mode=0o700,parents=False,exist_ok=False)
    flags=shlex.split(subprocess.run(['pkg-config','--cflags','--libs','libavcodec'],
        capture_output=True,text=True,check=True,timeout=10).stdout)
    source=Path(__file__).resolve().parent/'native'
    guard=output/'no-decoder.so';control=output/'decoder-control'
    for args in [
        ['cc','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',str(source/'publisher_no_decoder.c'),'-o',str(guard),*flags,'-ldl'],
        ['cc','-std=c11','-Wall','-Wextra','-Werror',str(source/'publisher_decoder_control.c'),'-o',str(control),*flags],
    ]: subprocess.run(args,check=True,capture_output=True,timeout=30)
    normal=subprocess.run([str(control)],capture_output=True,timeout=5)
    observed=subprocess.run([str(control)],env={**os.environ,'LD_PRELOAD':str(guard)},capture_output=True,timeout=5)
    if normal.returncode!=0 or observed.returncode!=91:
        raise RuntimeError('native decoder observer positive control failed')
    print('PASS: actual H264 decoder opens normally and the test observer detects it')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:build(args.output)
    except Exception:parser.exit(1,'Native decoder witness build/control failed; diagnostics withheld.\n')
