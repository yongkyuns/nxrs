"""Serial, local-only build/flash driver for the matched transport matrix.

Run from the repository with the pinned ESP environment already sourced.
Builds never overwrite frozen artifacts; flashing needs an explicit --measure.
Save a full local flash backup first, and restore it after measurements.
"""
import argparse
from pathlib import Path
import subprocess
import sys

from transport_pipeline_report import CASES
from relink_rust import lock_build_tree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASELINE_CASE = 'rust-owned-z'


def build_command(case, tree, sysroot, cargo_target, output, baseline_config):
    folder, language, ack_bytes = CASES[case]
    packet = ack_bytes == 28
    processing = case in ('c-packet-2', 'rust-packet-2', 'rust-packet-inplace-2')
    speed = case.endswith('-2')
    if language == 'c':
        command = [sys.executable, str(HERE / 'build_c.py'),
                   '--nuttx', str(tree / 'nuttx'), '--apps', str(tree / 'apps'),
                   '--baseline-config', str(baseline_config),
                   '--source', str(HERE / 'channel_scale_mq.c'),
                   '--platform', 'esp32s3-service-footprint', '--command', 'cq_c_scale',
                   '--stack-size', '8192', '--out', str(output), '--reuse-kernel']
        definitions = ['NXRS_CQ_DEVICE', 'NXRS_CQ_SYNCHRONIZED']
        if not processing: definitions.append('NXRS_CQ_WIRE_ONLY')
        if speed: definitions.append('NXRS_CQ_SPEED')
        if packet: definitions.append('NXRS_CQ_PACKET_SERVICE')
        sources = ['esp32s3_cycles.c', 'transport_gate.c']
        if packet:
            command += ['--target-c-header', str(HERE / 'pipeline_processing.h')]
        if processing:
            sources += ['payload_processing.c', 'pipeline_processing.c']
            definitions.append('NXRS_PAYLOAD_C_SPEED')
            for header in ('payload_processing.h',):
                command += ['--target-c-header', str(HERE / header)]
        for definition in definitions: command += ['--c-define', definition]
    else:
        command = [sys.executable, str(HERE / 'relink_rust.py'),
                   '--tree', str(tree), '--sysroot', str(sysroot),
                   '--cargo-target', str(cargo_target), '--out', str(output),
                   '--bin', 'cq-scale', '--command', 'cq_scale',
                   '--app-opt-level', '2' if speed else 'z']
        features = ['ffi-scale-entry', 'native-scale-worker', 'shared-mq-code', 'synchronized-scale']
        if case != BASELINE_CASE: features.append('borrowed-mq-io')
        if not processing: features.append('wire-only')
        if packet: features.append('packet-service')
        if case == 'rust-packet-inplace-2': features.append('packet-inplace-samples')
        for feature in features: command += ['--feature', feature]
        sources = ['native_thread.c', 'transport_gate.c']
    for source in sources: command += ['--target-c-source', str(HERE / source)]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=CASES, required=True)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--measure', action='store_true')
    parser.add_argument('--tree', type=Path, required=True)
    parser.add_argument('--sysroot', type=Path, required=True)
    parser.add_argument('--cargo-target', type=Path,
                        default=ROOT / 'target/service-footprint/cargo')
    parser.add_argument('--baseline-config', type=Path,
                        help='resolved Rust baseline .config required when selecting C cases')
    parser.add_argument('--out-root', type=Path, required=True)
    parser.add_argument('--port')
    parser.add_argument('--flasher', default='esptool.py')
    parser.add_argument('--runs', type=int, default=20)
    args = parser.parse_args()
    if args.runs < 1: parser.error('runs must be positive')
    if not (args.build or args.measure): parser.error('choose --build and/or --measure')
    if len(set(args.case)) != len(args.case): parser.error('duplicate case')
    if args.measure and not args.port: parser.error('--port is required with --measure')
    if any(CASES[case][1] == 'c' for case in args.case) and args.build and not args.baseline_config:
        parser.error('--baseline-config is required to build C comparison cases')
    out_root = args.out_root.resolve()
    if args.build:
        out_root.mkdir(parents=True, exist_ok=True)
    cases = list(args.case)
    baseline = BASELINE_CASE
    if args.build and baseline not in cases:
        cases.insert(0, baseline)
    if baseline in cases:
        cases.remove(baseline)
        cases.insert(0, baseline)
    for case in cases:
        folder, language, _ = CASES[case]
        directory = out_root / folder
        if args.build:
            if directory.exists():
                parser.error(f'fresh case output required: {directory}')
            if language == 'rust':
                # Use the native config tools; only application selection changes.
                with lock_build_tree(args.tree):
                    config = (args.tree / 'nuttx/.config').read_text()
                    if 'CONFIG_EXAMPLES_NXRS_STD_APP=y\n' not in config:
                        for command in (['kconfig-tweak', '--disable', 'CONFIG_EXAMPLES_NXRS_BENCH'],
                                        ['kconfig-tweak', '--enable', 'CONFIG_EXAMPLES_NXRS_STD_APP'],
                                        ['make', 'olddefconfig']):
                            subprocess.run(command, cwd=args.tree / 'nuttx', check=True)
            subprocess.run(build_command(case, args.tree, args.sysroot, args.cargo_target,
                                         directory, args.baseline_config), cwd=ROOT, check=True)
        if args.measure:
            app = 'cq_c_scale' if language == 'c' else 'cq_scale'
            prefix = 'CQ_C_' if language == 'c' else 'CQ_'
            common = [sys.executable, str(HERE / 'measure_device.py'),
                      '--port', args.port, '--flasher', args.flasher,
                      '--image', str(directory / f'{language}.merged.bin'), '--command-timeout', '30']
            subprocess.run([*common, '--out', str(directory / f'measure-{args.runs}'),
                            '--command', app + ' large', '--runs', str(args.runs),
                            '--expected-prefix', prefix + 'SCALE_PASS mode=large'], cwd=ROOT, check=True)
            # Reflash/reset: the OS maxused counter from traffic must not be
            # mistaken for the idle-thread baseline's high-water mark.
            subprocess.run([*common, '--out', str(directory / 'threads-3'),
                            '--command', app + ' threads', '--runs', '3',
                            '--expected-prefix', prefix + 'THREADS_PASS threads=20'], cwd=ROOT, check=True)

if __name__ == '__main__': main()
