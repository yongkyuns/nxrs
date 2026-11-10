#!/usr/bin/env python3
"""Build, capture and replay the shared workload with std and native transports."""
import argparse
import json
from pathlib import Path

import build
import capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    p = commands.add_parser('build')
    p.add_argument('--board', choices=build.BOARDS, required=True)
    p.add_argument('--runtime', choices=['nuttx', 'zephyr'], required=True)
    p.add_argument('--backend', choices=['std', 'native'], help='default: std on NuttX, native on Zephyr')
    p.add_argument('--tools', type=Path, required=True, help='private tool paths; see toolchains.example.toml')
    p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('run', help='capture only; does not flash or reset a device')
    p.add_argument('--build-directory', type=Path, required=True)
    p.add_argument('--case', choices=capture.cases(), required=True)
    p.add_argument('--port', required=True)
    p.add_argument('--session', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = commands.add_parser('replay')
    p.add_argument('--build-directory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = commands.add_parser('compare')
    p.add_argument('--nuttx-build', type=Path, required=True)
    p.add_argument('--nuttx-native-build', type=Path)
    p.add_argument('--zephyr-build', type=Path)
    p.add_argument('captures', type=Path, nargs='+')
    p = commands.add_parser('check-control', help='validate the NuttX substrate without physical captures')
    p.add_argument('--nuttx-build', type=Path, required=True)
    p.add_argument('--nuttx-native-build', type=Path, required=True)
    p = commands.add_parser('plan', help='16 cases per runtime, alternating runtime order each pass')
    p.add_argument('--passes', type=int, choices=range(1, 11), default=3)
    p.add_argument('--include-native-control', action='store_true', help='include the separate NuttX/native image')
    args = parser.parse_args()
    if args.operation == 'build':
        result = build.build(args)
        result = {k: result[k] for k in ('board', 'runtime', 'backend', 'build_id', 'image_file', 'elf_file')}
    elif args.operation == 'run':
        result = capture.capture(args)
    elif args.operation == 'replay':
        result = capture.replay(args.output, args.build_directory)
    elif args.operation == 'compare':
        result = capture.compare(args)
    elif args.operation == 'check-control':
        result = build.validate_nuttx_control(build.receipt(args.nuttx_build), build.receipt(args.nuttx_native_build))
    else:
        result = capture.plan(args.passes, args.include_native_control)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
