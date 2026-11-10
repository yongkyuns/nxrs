#!/usr/bin/env python3
"""Build separate P4 baseline/note images; never modify an installed SDK."""
import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('p4_reused_p2_builder', ROOT/'tests/event-path-perf/build.py')
p2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p2)
PACKAGE = 'nxrs-event-path-trace'
MANIFEST = HERE/'rust/Cargo.toml'
PLATFORM = 'esp32s3-service-footprint'
WRAP = '--wrap=nxsched_add_readytorun'
MEMORY_MAP = 'arch/xtensa/src/esp32s3/esp-hal-3rdparty/components/soc/esp32s3/include/soc/soc.h'
PINS = (
    'sched/sched/sched_addreadytorun.c', 'sched/sched/sched.h',
    'sched/sched/sched_switchcontext.c',
    'drivers/note/note_driver.c', 'include/nuttx/note/note_driver.h',
    'include/nuttx/sched.h', 'arch/xtensa/include/irq.h',
    'arch/xtensa/src/Makefile', 'arch/xtensa/src/esp32s3/esp32s3_spiram.c',
    'arch/xtensa/src/esp32s3/esp32s3_spiram.h', 'include/nuttx/irq.h',
    MEMORY_MAP,
)
IDENTITY = ('schema','package','mode','target','sdk','source_sha256','kernel_revision','apps_revision',
            'compiler_identity','std_array_sha256','std_library_tree_sha256','crossbeam',
            'build_nonce','platform','clock_request','boot_image_profile','build_environment')
ENABLED = ('SCHED_INSTRUMENTATION','SCHED_INSTRUMENTATION_SWITCH',
           'SCHED_INSTRUMENTATION_IRQHANDLER','DRIVERS_NOTE')
DISABLED = ('SCHED_INSTRUMENTATION_FILTER','SCHED_INSTRUMENTATION_CSECTION',
            'SCHED_INSTRUMENTATION_PREEMPTION','SCHED_INSTRUMENTATION_SYSCALL',
            'SCHED_INSTRUMENTATION_HEAP','SCHED_INSTRUMENTATION_DUMP',
            'SCHED_INSTRUMENTATION_SPINLOCKS','SCHED_INSTRUMENTATION_WDOG',
            'SCHED_INSTRUMENTATION_FUNCTION','DRIVERS_NOTERAM','DRIVERS_NOTELOG',
            'DRIVERS_NOTESNAP','DRIVERS_NOTEFILE','DRIVERS_NOTELOWEROUT')


def instrumentation_options(mode):
    # Hidden dependent symbols are omitted by Kconfig, not explicit "not set".
    # Disable the parent in baseline; verify every child after resolution.
    if mode == 'baseline':
        return ['--kconfig-disable','SCHED_INSTRUMENTATION']
    if mode != 'trace': raise ValueError('unknown instrumentation mode')
    options = []
    for name in ENABLED: options += ['--kconfig-enable',name]
    for name in DISABLED:
        # SYSCALL is itself hidden when ARCH_HAVE_SYSCALL_HOOKS is absent.
        if name != 'SCHED_INSTRUMENTATION_SYSCALL': options += ['--kconfig-disable',name]
    return options


def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def inventory(profile):
    values = p2.inventory(profile)
    for path in HERE.rglob('*'):
        if path.is_file() and (path.suffix in ('.py','.rs','.toml','.c','.h') or path.name == 'native-pins.json') and not {'target','__pycache__'} & set(path.parts):
            values[path.relative_to(ROOT).as_posix()] = p2.digest(path)
    return dict(sorted(values.items()))


def registry(metadata):
    locked = p2.locked_packages(MANIFEST)
    packages = {p['id']:p for p in metadata['packages']}
    root = next(p for p in packages.values() if Path(p['manifest_path']).resolve()==MANIFEST.resolve())
    if root['name'] != PACKAGE: raise ValueError('wrong P4 metadata root')
    nodes = {n['id']:n for n in metadata['resolve']['nodes']}
    channel = packages[next(d['pkg'] for d in nodes[root['id']]['deps'] if d['name']=='crossbeam_channel')]
    utils = packages[next(d['pkg'] for d in nodes[channel['id']]['deps'] if d['name']=='crossbeam_utils')]
    return {'schema':'event-path-registry-sources-v1', 'packages':[
        p2.registry_source(p,locked[p['name']]) for p in (channel,utils)]}


def build(out, mode):
    out = out.absolute()
    if out.exists(): raise ValueError('output must be new')
    profile = p2.resolve_platform(PLATFORM, ROOT)
    pins = json.loads((HERE/'native-pins.json').read_text())
    for name,expected in pins['prepared_patch_sha256'].items():
        if p2.digest(ROOT/name) != expected: raise ValueError('unaudited native patch')
    for key, submodule in [('kernel_revision','nuttx'),('apps_revision','nuttx-apps')]:
        if p2.run(['git','rev-parse',':external/'+submodule]) != pins[key]:
            raise ValueError('unaudited native revision: '+key)
    sdk = p2.baseline.sdk_for(profile)
    env = p2.build_environment(out,'2',os.environ,'native-counter')
    if env.get('EXTRALINKCMDS'): raise ValueError('unset competing native link overrides')
    env['EXTRALINKCMDS'] = WRAP
    before = inventory(profile)
    source = p2.baseline.discover_source(profile,sdk,env)
    out.mkdir(parents=True)
    record = {'schema':'event-path-trace-build-v1','package':PACKAGE,'mode':mode,
        'status':'failed','failure':'incomplete build','platform':PLATFORM,'target':profile['target'],'sdk':sdk,
        'clock_request':'native-counter','boot_image_profile':p2.boot_image.PROFILE,
        'source_revision':p2.run(['git','rev-parse','HEAD']), 'source_sha256':before,
        'kernel_revision':p2.run(['git','rev-parse',':external/nuttx']),
        'apps_revision':p2.run(['git','rev-parse',':external/nuttx-apps']),
        'build_nonce':secrets.token_hex(16),'build_environment':{key:env[key] for key in (
            'EXTRALINKCMDS','CARGO_PROFILE_RELEASE_OPT_LEVEL','CARGO_PROFILE_RELEASE_CODEGEN_UNITS',
            'CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS','CARGO_PROFILE_RELEASE_OVERFLOW_CHECKS',
            'CARGO_INCREMENTAL','NXRS_NUTTX_DIAGNOSTIC_UNWIND','NXRS_NUTTX_BUILD_JOBS','SOURCE_DATE_EPOCH') if key in env},
        'physical_timing_qualified':False,'priority_progress_qualified':False}
    try:
        prepared = out/'prepared'
        hook = p2.private_std.prepare(source,prepared,sdk,'latency')
        if hook['channel_instrumented'] or hook['hook_stage'] != 0: raise ValueError('instrumented std')
        record.update(compiler_identity=hook['compiler'],std_array_sha256=hook['selected']['sha256'],
                      std_library_tree_sha256=hook['library_tree_sha256'])
        env['RUSTC'] = str(prepared/'toolchain/bin/rustc')
        env['CARGO_BUILD_RUSTC'] = env['RUSTC'];env['RUSTC_BOOTSTRAP']='1'
        cargo = p2.run(['rustup','which','--toolchain','1.90.0','cargo'])
        result = subprocess.run([cargo,'metadata','--locked','--format-version','1','--manifest-path',str(MANIFEST)],
            env=env,cwd=ROOT,capture_output=True,text=True,check=True)
        (out/'cargo-metadata.log').write_text(result.stderr)
        metadata = json.loads(result.stdout); record['crossbeam']=registry(metadata)
        (out/'crossbeam-source.json').write_text(json.dumps(record['crossbeam'],indent=2)+'\n')
        record['build_id']=canonical({k:record[k] for k in IDENTITY})
        env['NXRS_EVENT_TRACE_BUILD_ID']=record['build_id']
        command = ['bash',ROOT/'tools/build-nuttx-std-app.sh','--app-manifest',MANIFEST,
            '--app-package',PACKAGE,'--bin','event-path-trace','--command','event_path_trace',
            '--priority','100','--stack-size','65536','--platform',PLATFORM,'--out',out/'firmware',
            '--abi-profile','active','--target-c-source',ROOT/'tests/std-channel-perf/native.c',
            '--target-c-source',HERE/'native.c']
        command += instrumentation_options(mode)
        record['build_command']=[str(x) for x in command]
        p2.run(command,env=env,log=out/'build.log')
        firmware=out/'firmware'
        p2.verify_selected_std(firmware,prepared,'latency')
        p2.verify_compiled_sources(firmware,record['crossbeam'])
        if registry(metadata)!=record['crossbeam']:raise ValueError('registry source drift')
        config=firmware/'resolved.config'
        resolved=p2.baseline.config_values(config)
        for name in ENABLED:
            if (resolved.get('CONFIG_'+name)=='y') != (mode=='trace'):
                raise ValueError('instrumentation config mismatch: '+name)
        for name in DISABLED:
            if resolved.get('CONFIG_'+name)=='y': raise ValueError('unexpected note path: '+name)
        p2.validate_configuration(config,'baseline','native-counter',PLATFORM)
        original=firmware/'nuttx'/profile['image']
        image,fields,boot_artifacts=p2.boot_image.prepare(firmware,original,out)
        record.update(fields)
        elf=firmware/'nuttx/nuttx'
        record.update(image_file=p2.relative(image,out),image_sha256=p2.digest(image),
            elf_file=p2.relative(elf,out),elf_sha256=p2.digest(elf),
            config_file=p2.relative(config,out),config_sha256=p2.digest(config))
        # Keep exact native sources used for the callback/ready-edge argument.
        native_sources={}
        for name in PINS:
            src=firmware/'nuttx'/name;dest=out/'native-sources'/name
            if p2.digest(src) != pins['files'][name]:
                raise ValueError('unaudited native source: '+name)
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
            native_sources[name]=p2.digest(dest)
        record['native_source_sha256']=native_sources
        sdk_header=(firmware/'nuttx'/MEMORY_MAP).read_text()
        translated=(HERE/'memory-bounds.h').read_text()
        for suffix in ('LOW','HIGH'):
            original=re.search(r'^#define SOC_DRAM_'+suffix+r'\s+(0x[0-9A-Fa-f]+)\s*$',sdk_header,re.M)
            selected=re.search(r'^#define P4_DRAM_'+suffix+r'\s+(0x[0-9A-Fa-f]+)\s*$',translated,re.M)
            if not original or not selected or int(original[1],16)!=int(selected[1],16):
                raise ValueError('memory boundary translation mismatch')
        nm=shutil.which('xtensa-esp32s3-elf-nm');objdump=shutil.which('xtensa-esp32s3-elf-objdump')
        symbols=p2.run([nm,'--defined-only',elf]);(out/'symbols.txt').write_text(symbols)
        for symbol in ['__wrap_nxsched_add_readytorun','nxsched_add_readytorun','trace_register']:
            if not any(line.endswith(' '+symbol) for line in symbols.splitlines()):raise ValueError('missing wrapper symbol '+symbol)
        disassembly=p2.run([objdump,'-d',elf]);(out/'disassembly.txt').write_text(disassembly)
        # Xtensa long calls can load a literal address before callx8. Retain
        # references rather than mislabeling that literal as a direct call.
        references=[line for line in disassembly.splitlines()
                    if re.match(r'^\s*[0-9a-f]+:\s',line) and '<__wrap_nxsched_add_readytorun>' in line]
        if not references:raise ValueError('ready wrapper has no linked references')
        record['ready_wrapper_references']=len(references)
        paths=[image,elf,config,firmware/'std-source.json',firmware/'std-patch.json',
            prepared/'std-channel-hook.json',prepared/'std-library-sources.json',
            firmware/'abi-report.json',firmware/'cargo-messages.jsonl',out/'crossbeam-source.json',
            out/'symbols.txt',out/'disassembly.txt',*[out/'native-sources'/name for name in PINS],*boot_artifacts]
        record['artifacts_sha256']={p2.relative(p,out):p2.digest(p) for p in paths}
        p2.boot_image.verify(out,record)
        if inventory(profile)!=before:raise ValueError('build inputs changed')
        record.update(status='complete',failure=None)
    except Exception as error:
        record['failure']=f'{type(error).__name__}: {error}'
        raise
    finally:
        (out/'build.json').write_text(json.dumps(record,indent=2)+'\n')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--mode',choices=['baseline','trace'],required=True)
    args=parser.parse_args()
    build(args.out,args.mode)
