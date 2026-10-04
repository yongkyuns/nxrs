"""Serial local controls: alternate C and Rust inside one firmware image.

Use separate primary images for footprint comparisons. These mixed images
only control timing for boot state, command order and common kernel helpers.
Back up the complete flash first and restore it after the local experiment.
"""
import argparse
from pathlib import Path
import subprocess
import sys

from run_transport_matrix import HERE, ROOT


def build_command(kind, tree, sysroot, cargo_target, output):
    command = [sys.executable, str(HERE / 'relink_rust.py'),
               '--tree', str(tree), '--sysroot', str(sysroot),
               '--cargo-target', str(cargo_target), '--out', str(output),
               '--bin', 'cq-scale', '--command', 'cq_pair', '--app-opt-level', '2']
    features = ['ffi-scale-entry', 'native-scale-worker', 'shared-mq-code',
                'synchronized-scale', 'borrowed-mq-io', 'paired-scale-control',
                'wire-only' if kind == 'wire' else 'packet-service']
    if kind == 'packet-inplace': features.append('packet-inplace-samples')
    definitions = ['NXRS_CQ_DEVICE', 'NXRS_CQ_SYNCHRONIZED',
                   'NXRS_CQ_EMBED_CONTROL', 'NXRS_CQ_SPEED']
    sources = ['native_thread.c', 'transport_gate.c', 'channel_scale_mq.c']
    if kind != 'wire':
        definitions += ['NXRS_CQ_PACKET_SERVICE', 'NXRS_PAYLOAD_C_SPEED']
        sources += ['payload_processing.c', 'pipeline_processing.c']
        for header in ('payload_processing.h', 'pipeline_processing.h'):
            command += ['--target-c-header', str(HERE / header)]
    else:
        definitions += ['NXRS_CQ_WIRE_ONLY']
    for feature in features: command += ['--feature', feature]
    for definition in definitions: command += ['--c-define', definition]
    for source in sources: command += ['--target-c-source', str(HERE / source)]
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=('wire', 'packet', 'packet-inplace'), required=True)
    parser.add_argument('--tree', type=Path, required=True)
    parser.add_argument('--sysroot', type=Path, required=True)
    parser.add_argument('--cargo-target', type=Path,
                        default=ROOT / 'target/service-footprint/cargo')
    parser.add_argument('--out-root', type=Path, required=True)
    parser.add_argument('--port')
    parser.add_argument('--flasher', default='esptool.py')
    parser.add_argument('--runs', type=int, default=10)
    parser.add_argument('--measure', action='store_true', help='explicitly enable device flashing')
    args = parser.parse_args()
    if args.runs < 1: parser.error('runs must be positive')
    if len(set(args.case)) != len(args.case): parser.error('duplicate case')
    if args.measure and not args.port: parser.error('--port is required with --measure')
    out_root = args.out_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    for kind in args.case:
        directory = out_root / f'transport-paired-{kind}-2-v1'
        if directory.exists(): parser.error(f'fresh case output required: {directory}')
        subprocess.run(build_command(kind, args.tree, args.sysroot, args.cargo_target, directory),
                       cwd=ROOT, check=True)
        if args.measure:
            subprocess.run([sys.executable, str(HERE / 'measure_device.py'),
                            '--port', args.port, '--flasher', args.flasher,
                            '--image', str(directory / 'rust.merged.bin'),
                            '--out', str(directory / f'measure-{args.runs}'),
                            '--command', 'cq_pair', '--runs', str(args.runs),
                            '--command-timeout', '30', '--expected-prefix', 'CQ_PAIR_PASS batches=4'],
                           cwd=ROOT, check=True)


if __name__ == '__main__': main()
