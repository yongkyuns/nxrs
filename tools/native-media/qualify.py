#!/usr/bin/env python3
"""Instrument the pinned native image2pipe/MJPEG implementation; export no media."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('parser_runner', ROOT / 'gateway/fuzz/run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FLAGS = ['-O1', '-g', '-fno-omit-frame-pointer', '-fsanitize=address,undefined',
         '-fno-sanitize-recover=all']
COVERAGE = ['-fsanitize-coverage=inline-8bit-counters,pc-table,trace-cmp']
INPUTS = ('tools/native-media/qualify.py', 'tools/native-media/README.md',
          'security/native-media-dependencies.json', '.github/workflows/security.yml',
          'gateway/tests/native/jpeg_fuzz.c', 'gateway/tests/test_native_media_evidence.py',
          'gateway/fuzz/run.py', 'gateway/fuzz/Cargo.lock', 'tests/boot/admission/sanitizer_probe.c',
          'tests/device-identity/frame.jpg', 'gateway/ingest/src/protocol.rs',
          'gateway/ingest/src/runner.rs', 'gateway/activity/src/worker.rs')


def require(value):
    if not value:
        raise ValueError('native media qualification refused')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(archive, destination, root):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        require(0 < len(members) <= 20000 and sum(m.size for m in members) <= 128 * 1024 * 1024)
        seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            require(name.parts and name.parts[0] == root and not name.is_absolute()
                    and '..' not in name.parts and name.as_posix() == member.name.rstrip('/')
                    and member.name not in seen and (member.isfile() or member.isdir()))
            seen.add(member.name)
        source.extractall(destination, members=members, filter='data')
    return destination / root


def instrumentation(symbols):
    require(len(symbols) <= 262144)
    required = ('__asan_', '__ubsan_handle_', '__sanitizer_cov_8bit_counters_init',
                '__sanitizer_cov_pcs_init', '__sanitizer_cov_trace_const_cmp')
    require(all(name in symbols for name in required))
    return True


def snapshot():
    return {name: digest(ROOT / name) for name in INPUTS}


def run(args):
    require(sys.version_info >= (3, 12) and sys.platform in ('linux', 'darwin'))
    require(platform.machine() in ('x86_64', 'arm64', 'aarch64'))
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip())
    before = snapshot()
    require(not args.work.exists())
    args.work.mkdir(mode=0o700)
    os.umask(0o077)
    environment = {'PATH': os.environ['PATH'], 'LANG': 'C', 'LC_ALL': 'C',
                   'ASAN_OPTIONS': 'detect_leaks=0:abort_on_error=1:symbolize=0:disable_coredump=1',
                   'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=0'}
    sdk_settings = None
    if sys.platform == 'darwin':
        if 'DEVELOPER_DIR' in os.environ:
            environment['DEVELOPER_DIR'] = os.environ['DEVELOPER_DIR']
        environment['SDKROOT'] = subprocess.check_output(
            ['xcrun', '--sdk', 'macosx', '--show-sdk-path'], env=environment,
            text=True, timeout=10).strip()
        sdk_settings = digest(Path(environment['SDKROOT']) / 'SDKSettings.json')
    def tool(name):
        if sys.platform == 'darwin' and name in ('clang', 'clang++', 'nm', 'ar'):
            path = subprocess.check_output(['xcrun', '--find', name], env=environment, text=True, timeout=10).strip()
        else:
            path = shutil.which(name)
        require(bool(path))
        result = Path(path).absolute()
        require(result.is_file())
        return result
    cc, cxx, nm, ar, make = [tool(name) for name in (args.cc, args.cxx, 'nm', 'ar', 'make')]
    compiler = subprocess.check_output([cc, '--version'], env=environment, text=True, timeout=10)
    require(len(compiler) <= 4096 and re.search(r'(Apple clang version 21\.|clang version 18\.)', compiler))
    tools = {name: digest(path) for name, path in [('cc', cc), ('cxx', cxx), ('nm', nm), ('ar', ar), ('make', make)]}

    def execute(command, phase, seconds=600, cwd=None):
        code, raw = runner.execute(command, args.work, phase, environment, seconds, cwd=cwd)
        require(code == 0)
        return raw

    pins = json.loads((ROOT / 'security/native-media-dependencies.json').read_text())
    sources = {}
    for name in ('ffmpeg', 'libfuzzer'):
        pin = pins[name]
        archive = args.work / (name + '.tar')
        if args.archives:
            original = args.archives / (name + '.tar')
            require(original.is_file() and not original.is_symlink())
            with original.open('rb') as source:
                raw = source.read(32 * 1024 * 1024 + 1)
        else:
            with urllib.request.urlopen(pin['url'], timeout=45) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
        require(len(raw) <= 32 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == pin['sha256'])
        archive.write_bytes(raw)
        sources[name] = extract(archive, args.work, pin['root'])
    ffmpeg = sources['ffmpeg']
    build = args.work / 'build'
    build.mkdir()
    configuration = ['--disable-everything', '--disable-autodetect', '--disable-programs',
                     '--disable-doc', '--disable-network', '--disable-asm', '--disable-shared',
                     '--enable-static', '--disable-avdevice', '--disable-avfilter',
                     '--disable-swresample', '--disable-swscale', '--enable-decoder=mjpeg',
                     '--enable-parser=mjpeg', '--enable-demuxer=image2pipe',
                     '--cc=' + str(cc), '--cxx=' + str(cxx), '--ar=' + str(ar),
                     '--extra-cflags=' + ' '.join(FLAGS + COVERAGE),
                     '--extra-ldflags=-fsanitize=address,undefined',
                     '--optflags=-O1']
    print('NATIVE_MEDIA_PHASE configure-and-build', flush=True)
    execute([ffmpeg / 'configure', *configuration], 'configure', cwd=build)
    libraries = ['libavformat/libavformat.a', 'libavcodec/libavcodec.a', 'libavutil/libavutil.a']
    execute([make, '-j2', *libraries], 'native-build', cwd=build)
    objects = ['libavcodec/mjpegdec.o', 'libavcodec/mjpeg_parser.o',
               'libavformat/img2dec.o']
    object_evidence = {}
    for index, name in enumerate(objects):
        symbols = execute([nm, '-u', build / name], 'symbols-' + str(index), 10).decode()
        instrumentation(symbols)
        object_evidence[name] = digest(build / name)
    negative = args.work / 'uninstrumented-mjpeg.o'
    execute([cc, '-O1', '-DHAVE_AV_CONFIG_H', '-I' + str(build), '-I' + str(ffmpeg),
             '-c', ffmpeg / 'libavcodec/mjpegdec.c', '-o', negative], 'uninstrumented-control')
    symbols = execute([nm, '-u', negative], 'uninstrumented-symbols', 10).decode()
    try:
        instrumentation(symbols)
    except ValueError:
        pass
    else:
        raise ValueError('uninstrumented object accepted')

    print('NATIVE_MEDIA_PHASE engine-and-controls', flush=True)
    engine = args.work / 'engine'
    engine.mkdir()
    cpp = sorted((sources['libfuzzer'] / 'libfuzzer').glob('*.cpp'))
    require(20 <= len(cpp) <= 40)
    execute([cxx, '-std=c++17', '-O1', '-g', '-fno-omit-frame-pointer', '-pthread',
             *FLAGS[3:], '-c', *cpp], 'engine-build', cwd=engine)
    engine_objects = sorted(engine.glob('*.o'))
    require(len(engine_objects) == len(cpp))
    include = ['-I' + str(build), '-I' + str(ffmpeg)]
    executed = {}
    for control in (False, True):
        name = 'decode-control' if control else 'native-jpeg-fuzz'
        obj = args.work / (name + '.o')
        execute([cc, '-std=c11', '-Wall', '-Wextra', '-Werror', *FLAGS, *COVERAGE,
                 *include, *(['-DNATIVE_DECODE_CONTROL'] if control else []),
                 '-c', ROOT / 'gateway/tests/native/jpeg_fuzz.c', '-o', obj], name + '-compile')
        engine_link = [p for p in engine_objects if not control or p.name != 'FuzzerMain.o']
        binary = args.work / name
        execute([cxx, *FLAGS, obj, *engine_link, *(build / p for p in libraries),
                 '-pthread', '-lm', '-o', binary], name + '-link')
        executed[name] = binary
    probe = args.work / 'sanitizer-probe'
    execute([cc, *FLAGS, ROOT / 'tests/boot/admission/sanitizer_probe.c', '-o', probe], 'probe-build')
    for name, command, diagnostic in [
            ('asan', [probe, 'asan'], b'ERROR: AddressSanitizer: heap-buffer-overflow'),
            ('ubsan', [probe], b'runtime error: shift exponent 32 is too large')]:
        code, raw = runner.execute(command, args.work, name + '-probe', environment, 15)
        require(code != 0 and diagnostic in raw)
    executed['sanitizer-probe'] = probe

    print('NATIVE_MEDIA_PHASE actual-image-controls', flush=True)
    seed = (ROOT / pins['seed']['path']).read_bytes()
    require(4 < len(seed) <= 153600)
    malformed = args.work / 'malformed.jpg'
    malformed.write_bytes(b'\xff\xd8\xff\xd9')
    for mode, path in [('valid', ROOT / pins['seed']['path']), ('invalid', malformed)]:
        raw = execute([executed['decode-control'], mode, path], 'decode-' + mode, 15)
        expected = 'NATIVE_JPEG_CONTROL_PASS ' + mode
        if mode == 'valid':
            expected += (' frames=1 width=' + str(pins['seed']['width'])
                         + ' height=' + str(pins['seed']['height'])
                         + ' luma=' + pins['seed']['luma_fnv1a64'])
        require(raw == (expected + '\n').encode())
    corpus = args.work / 'corpus'
    corpus.mkdir()
    for name, data in [('valid', b'\x80' + seed), ('two-frames', b'\xff' + seed + seed),
                       ('truncated', b'\x01' + seed[:-16]), ('invalid', b'\x00\xff\xd8\xff\xd9')]:
        (corpus / name).write_bytes(data)
    artifacts = args.work / 'artifacts'
    artifacts.mkdir()
    hashes = {name: digest(path) for name, path in executed.items()}
    print('NATIVE_MEDIA_PHASE coverage-guided-native-decode', flush=True)
    raw = execute([executed['native-jpeg-fuzz'], corpus, '-runs=100000', '-seed=1',
                   '-max_total_time=60', '-timeout=5', '-rss_limit_mb=1024', '-max_len=524289',
                   '-print_final_stats=1', '-artifact_prefix=' + str(artifacts) + '/'],
                  'native-fuzz', 80)
    counts = runner.statistics(raw)
    require(hashes == {name: digest(path) for name, path in executed.items()})
    require(before == snapshot() and not subprocess.check_output(
        ['git', 'status', '--porcelain'], cwd=ROOT).strip())
    require(tools == {name: digest(path) for name, path in [('cc', cc), ('cxx', cxx), ('nm', nm), ('ar', ar), ('make', make)]})
    report = dict(schema=1, scope='pinned-native-image2pipe-mjpeg-component-fuzzing', qualified=True,
                  checkout=dict(clean=True, commit=head), inputs=before, compiler='\n'.join(line for line in compiler.strip().splitlines() if not line.startswith('InstalledDir:')), 
                  compiler_tools=tools, sdk_settings_sha256=sdk_settings, upstream=pins, configure_arguments=configuration,
                  native_libraries={p: digest(build / p) for p in libraries},
                  instrumented_objects=object_evidence,
                  executables=hashes, counts=counts, host_machine=platform.machine(),
                  native_asan=True, native_ubsan=True, native_coverage_guidance=True,
                  real_asan_activation_control=True, real_ubsan_activation_control=True,
                  uninstrumented_native_object_refused=True,
                  uninstrumented_native_object_sha256=digest(negative), valid_jpeg_decode_control=True,
                  malformed_jpeg_refused=True, valid_luma_fnv1a64=pins['seed']['luma_fnv1a64'],
                  maximum_stream_bytes=524288, maximum_packet_bytes=153600,
                  maximum_decoded_pixels=2073600, maximum_native_allocation_bytes=67108864,
                  maximum_input_packets=8, maximum_output_frames_per_receive=16, demux_read_bytes=4096,
                  maximum_seconds=60, maximum_rss_mb=1024, assembly_instrumented=False,
                  installed_distribution_qualified=False, all_native_parsers_qualified=False,
                  device_runtime_qualified=False, physical_qualified=False, fips_validated=False,
                  native_advisory_closure=False, deployment_permitted=False, private_inputs_used=False)
    # Absolute private work paths are build mechanics, not public evidence.
    report['configure_arguments'] = [v.replace(str(args.work), '<private-work>')
                                     .replace(str(cc), '<cc>').replace(str(cxx), '<cxx>').replace(str(ar), '<ar>')
                                     for v in configuration]
    raw = (json.dumps(report, sort_keys=True, indent=2) + '\n').encode()
    require(len(raw) <= 65536)
    (args.work / 'qualification.json').write_bytes(raw)
    print('PASS: actual native image2pipe/MJPEG fuzzing and sanitizer controls; bounded public receipt only',
          flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--archives', type=Path)
    parser.add_argument('--cc', default='clang')
    parser.add_argument('--cxx', default='clang++')
    arguments = parser.parse_args()
    arguments.work = arguments.work.resolve()
    if arguments.archives:
        arguments.archives = arguments.archives.resolve()
    try:
        run(arguments)
    except Exception:
        parser.exit(1, 'Native media qualification refused; private build/log/corpus/crash files remain local.\n')
