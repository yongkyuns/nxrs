#!/usr/bin/env python3
"""Exercise partial-link pruning with real GNU tools, including unsafe imports."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

from link import arguments, prune_discarded_undefined


def run(prefix):
    env = dict(os.environ, LC_ALL='C')
    def output(tool, *args):
        return subprocess.check_output([prefix + tool, *map(str, args)], text=True, env=env, stderr=subprocess.PIPE)

    spec = importlib.util.spec_from_file_location('abi', Path(__file__).with_name('check-abi.py'))
    abi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(abi)
    with tempfile.TemporaryDirectory(prefix='nxrs-link-') as folder:
        root = Path(folder)
        for mode in ('dead', 'live', 'address', 'weak', 'strip-live'):
            directory = root / mode
            directory.mkdir()
            source = 'extern int poll(void);\nextern int live_api(void);\n'
            if mode == 'dead':
                source += 'int unused(void) { return poll(); }\nint main(void) { return live_api(); }\n'
            elif mode == 'address':
                source += 'int (* volatile callback)(void) = poll;\nint main(void) { return callback(); }\n'
            elif mode == 'weak':
                source += '#pragma weak poll\nint main(void) { return poll ? poll() : 0; }\n'
            else:
                source += 'int main(void) { return poll(); }\n'
            c_file, obj, binary = (directory / name for name in ('probe.c', 'probe.o', 'partial.elf'))
            c_file.write_text(source)
            output('gcc', '-g', '-ffunction-sections', '-fdata-sections', '-fno-pic',
                   '-fno-asynchronous-unwind-tables', '-c', c_file, '-o', obj)
            args, deferred = arguments([str(obj), '-o', str(binary)])
            assert not deferred
            output('ld', *args)
            defined = output('nm', '--defined-only', '-P', binary)
            debug = directory / 'debug-before.bin'
            output('objcopy', '--dump-section', '.debug_info=' + str(debug), binary, directory / 'debug-copy.elf')
            before = binary.read_bytes()
            try:
                if mode == 'strip-live':
                    output('objcopy', '--strip-symbol=poll', binary, directory / 'rejected.elf')
                else:
                    prune_discarded_undefined(binary, prefix, directory)
            except subprocess.CalledProcessError:
                assert mode == 'strip-live', 'pruning unexpectedly failed'
                assert binary.read_bytes() == before, 'failed pruning changed original ELF'
                print('PASS: objcopy refuses to strip a referenced symbol; original ELF unchanged')
                continue
            assert mode != 'strip-live', 'removed a live relocation target'
            assert output('nm', '--defined-only', '-P', binary) == defined
            after_debug = directory / 'debug-after.bin'
            output('objcopy', '--dump-section', '.debug_info=' + str(after_debug), binary, directory / 'debug-copy-after.elf')
            assert after_debug.read_bytes() == debug.read_bytes(), 'debug information changed'
            symbols = output('nm', binary)
            report = json.loads((directory / 'rust-symbol-pruning.json').read_text())
            if mode == 'dead':
                assert report['removed_local_undefined'] == ['poll'], 'missing GNU residue witness'
                assert 'poll' not in abi.check_imports(symbols)
                assert 'live_api' in abi.check_imports(symbols)
            else:
                assert not report['removed_local_undefined'], 'live import selected for removal'
                try:
                    abi.check_imports(symbols)
                except AssertionError:
                    pass
                else:
                    raise AssertionError('live poll reference evaded ABI guard')
            print(f'PASS: {mode}; defined symbols/debug data preserved; import guard intact')

    callback_roots(prefix, env)


def callback_roots(prefix, environment):
    """A callback referenced only by a later SDK must survive partial Rust GC.

    These are real GNU compilation/link controls, not a replacement SDK or
    evidence that a particular NuttX kernel has executed.
    """
    with tempfile.TemporaryDirectory(prefix='nxrs-sdk-callback-link-') as folder:
        root = Path(folder)
        caller = root / 'caller.c'
        caller.write_text('extern int native_operation(void);\n'
                          'int nxrs_required_sdk_callback(void) { return 23; }\n'
                          'int main(void) { return native_operation(); }\n')
        sdk = root / 'sdk.c'
        sdk.write_text('extern int nxrs_required_sdk_callback(void);\n'
                       'int native_operation(void) { return nxrs_required_sdk_callback(); }\n')
        for path in (caller, sdk):
            subprocess.run([prefix + 'gcc', '-ffunction-sections', '-fdata-sections', '-fno-pic',
                            '-fno-asynchronous-unwind-tables', '-c', str(path), '-o', str(path.with_suffix('.o'))],
                           env=environment, check=True, capture_output=True)
        for retained in (False, True):
            partial = root / ('retained.o' if retained else 'discarded.o')
            args, deferred = arguments([str(caller.with_suffix('.o')), '-o', str(partial),
                                       *(['-u', 'nxrs_required_sdk_callback'] if retained else [])])
            assert not deferred
            subprocess.run([prefix + 'ld', *args], env=environment, check=True, capture_output=True)
            final = root / ('retained.elf' if retained else 'discarded.elf')
            result = subprocess.run([prefix + 'ld', '--entry=main', str(partial), str(sdk.with_suffix('.o')),
                                     '-o', str(final)], env=environment, capture_output=True, text=True)
            if retained:
                assert result.returncode == 0, 'rooted native SDK callback did not resolve'
                symbols = subprocess.check_output([prefix + 'nm', str(final)], env=environment, text=True)
                assert ' T nxrs_required_sdk_callback' in symbols
            else:
                assert result.returncode != 0 and 'nxrs_required_sdk_callback' in result.stderr, 'unrooted callback control unexpectedly survived'
        print('PASS: actual later-SDK callback fails without a partial-link root and resolves with it')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default='')
    run(parser.parse_args().prefix)
