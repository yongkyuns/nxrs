#!/usr/bin/env python3
"""Build isolated, frozen service-network images with the existing pinned tools."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
from rtos_harness.images import (assert_stack_section, image_header, parse_sections,
                                 esp_idf_section_accounting as section_accounting)
from rtos_harness import zephyr

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

sys.path.insert(0, str(HERE.parent/'service-footprint'))
nuttx_helpers = load('event_nuttx_relink', HERE.parent/'service-footprint/relink_rust.py')
config_helpers = load('event_nuttx_config', HERE.parent/'rtos-bench/build.py')
minimal = load('event_nuttx_minimal', HERE/'nuttx_profile.py')

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def command(argv, out, name, cwd=ROOT, env=None):
    result = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (out/name).write_text(result.stdout)
    result.check_returncode()
    return result.stdout

def inventory(nuttx_profile='baseline'):
    # Only build inputs: measurement tools and documentation may change while
    # a build runs, but firmware inputs must stay frozen through completion.
    names = ('contract.h','platform.h','runtime.h','clock.h','core.c','core.rs','runtime.c',
             'entry.c','nuttx.rs','platform_nuttx.c','platform_zephyr.c',
             'zephyr_main.c','embassy.rs','owned_slot.rs','telemetry.rs',
             'controls.h','controls.rs','scheduling.rs','saturation.h','saturation.c',
             'hal.h','hal_nuttx.c','hal_zephyr.c','hal.rs','timer-1ms.conf',
             'Cargo.toml','Cargo.lock','build.rs','CMakeLists.txt',
             '.cargo/config.toml','build.py')
    files = [HERE/name for name in names]
    files += [HERE.parent/'zephyr-comparison'/n for n in ('prj.conf','app.overlay')]
    if nuttx_profile == 'minimal':
        files += [HERE/n for n in ('nuttx_console.c','nuttx-minimal.conf','nuttx_profile.py')]
    files += [HERE.parent/'embassy-comparison/stack.x',
              HERE.parent/'service-footprint/Cargo.toml',
              HERE.parent/'service-footprint/relink_rust.py',
              HERE.parent/'service-footprint/build_c.py',
              HERE.parent/'rtos-bench/build.py',
              HERE.parent/'rtos_harness/images.py', HERE.parent/'rtos_harness/zephyr.py']
    return {os.path.relpath(p,HERE):digest(p) for p in sorted(files)}

def build(args):
    timer_ms = getattr(args, 'timer_ms', 10)
    scheduling_policy = getattr(args, 'embassy_scheduling', 'event')
    work_mode = getattr(args, 'work_mode', 'monolithic')
    nuttx_profile = getattr(args, 'nuttx_profile', 'baseline')
    if args.platform != 'embassy' and (scheduling_policy != 'event' or work_mode != 'monolithic'):
        raise ValueError('cooperative controls apply only to Embassy')
    if work_mode == 'chunked' and scheduling_policy != 'budget':
        raise ValueError('chunked work requires the budget scheduling policy')
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    record={'schema':1,'status':'failed','failure':'build incomplete',
            'platform':args.platform,'layout':args.layout,'configuration':{
                'cpu_mhz':240,'cores':1,'flash_mode':'DIO','flash_frequency_mhz':40,
                'queues':20 if args.layout=='one' else 60,'slots':480,'event_bytes':64,
                'duration_us':2000000,'drain_us':500000,'handler_work':'contract.h and README.md',
                'timebase':'ESP32-S3 SYSTIMER Unit0 (16 MHz), scaled by 15',
                'idle_backend':'WAITI' if args.platform=='embassy' else 'native RTOS',
                'publication_timer_resolution_ms':timer_ms,'application_opt_level':'O2',
                'work_short_iterations':10000,'work_long_iterations':400000,
                'work_medium_iterations':100000,
                'work_service':0,'work_kind':1,'hal_gpio':2,
                'controls':'normal,burst,work-short,work-medium,work-long,hal,saturation,io-wait',
                'embassy_scheduling':scheduling_policy if args.platform=='embassy' else 'native',
                'work_mode':work_mode,'scheduling_budget_events':4,'scheduling_budget_us':500,
                'work_chunk_iterations':10000,'io_wait_us':3000},
            'source_sha256':inventory(nuttx_profile),'artifacts':{}}
    mailbox=int(args.layout=='one')
    try:
        if args.platform=='embassy':
            cargo_target=args.cargo_target.resolve()
            argv=['cargo','+esp','build','--locked','--offline','--release','--target-dir',cargo_target]
            features = ([] if not mailbox else ['mailbox']) + ([] if timer_ms == 10 else ['timer-1ms'])
            if scheduling_policy != 'event': features += ['scheduling-'+scheduling_policy]
            if work_mode == 'chunked': features += ['work-chunked']
            if features: argv+=['--features',','.join(features)]
            env=os.environ.copy()
            env['PATH']=str(args.readelf.resolve().parent)+os.pathsep+env.get('PATH','')
            command(argv,out,'build.log',HERE,env)
            elf=cargo_target/'xtensa-esp32s3-none-elf/release/embassy-services'
            shutil.copy2(elf,out/'app.elf')
            command([args.espflash,'save-image','--chip','esp32s3','--flash-mode','dio',
                '--flash-freq','40mhz','--flash-size','16mb','--merge','--skip-padding',
                '--skip-update-check',out/'app.elf',out/'image.bin'],out,'image.log')
            record['image_header']=image_header(out/'image.bin')
            record['toolchain']={name:command([name,'+esp','--version'],out,name+'.log',HERE).strip()
                                 for name in ('rustc','cargo')}
            readelf=args.readelf
        elif args.platform=='zephyr-c':
            stage=out/'zephyr-build'
            record['revisions'] = zephyr.input_revisions(args)
            env=os.environ.copy()
            env['ZEPHYR_BASE']=str(args.zephyr.resolve())
            env['PATH']=str(args.zephyr_python.absolute().parent)+os.pathsep+env.get('PATH','')
            command(['cmake','-GNinja','-S',HERE,'-B',stage,
                '-DBOARD='+zephyr.BOARD,'-DZEPHYR_BASE='+str(args.zephyr.resolve()),
                '-DZephyr_DIR='+str(args.zephyr.resolve()/'share/zephyr-package/cmake'),
                '-DZEPHYR_MODULES='+str(args.espressif.resolve())+';'+str(args.xtensa.resolve()),
                '-DZEPHYR_SDK_INSTALL_DIR='+str(args.sdk.resolve()),
                '-DPython3_EXECUTABLE='+str(args.zephyr_python.absolute()),
                '-DES_MAILBOX='+str(mailbox),'-DES_TIMER_MS='+str(timer_ms),
                *([] if timer_ms == 10 else ['-DEXTRA_CONF_FILE='+str(HERE/'timer-1ms.conf')])],out,'configure.log',env=env)
            command(['cmake','--build',stage,'--parallel','2'],out,'build.log',env=env)
            config=(stage/'zephyr/.config').read_text()
            record['resolved_configuration']=zephyr.assert_config(config)
            if record['resolved_configuration'].get('CONFIG_SYS_CLOCK_TICKS_PER_SEC') != str(1000 // timer_ms):
                raise ValueError('Zephyr publication timer resolution does not match the control')
            shutil.copy2(stage/'zephyr/zephyr.elf',out/'app.elf')
            shutil.copy2(stage/'zephyr/zephyr.bin',out/'image.bin')
            shutil.copy2(stage/'zephyr/.config',out/'resolved.config')
            readelf=args.sdk/'xtensa-espressif_esp32s3_zephyr-elf/bin/xtensa-espressif_esp32s3_zephyr-elf-readelf'
        else:
            nuttx=args.nuttx_tree.resolve()/'nuttx';apps=args.nuttx_tree.resolve()/'apps'
            env=os.environ.copy()
            esptool=getattr(args,'esptool',None) or ROOT/'target/zephyr-python/bin/esptool.py'
            env['PATH']=os.pathsep.join((str(args.readelf.resolve().parent),
                                       str(esptool.absolute().parent),
                                       '/usr/local/opt/gnu-sed/libexec/gnubin',env.get('PATH','')))
            stage=out/'nuttx-build'
            definitions=['ES_SPEED=1','ES_MAILBOX='+str(mailbox),'ES_TIMER_MS='+str(timer_ms)]
            headers=[HERE/n for n in ('contract.h','platform.h','runtime.h','clock.h','controls.h','saturation.h','hal.h')]
            helpers=[HERE/n for n in ('runtime.c','core.c','platform_nuttx.c','saturation.c','hal_nuttx.c')]
            if nuttx_profile == 'minimal':
                minimal.validate(nuttx/'.config', args.matched_baseline)
                helpers += [HERE/'nuttx_console.c']
                record['configuration'].update(nuttx_profile='minimal', console='event',
                                                kernel_opt_level='Os', psram=False)
            lock=nuttx_helpers.lock_build_tree(args.nuttx_tree.resolve())
            try:
                if config_helpers.config_identity(nuttx/'.config')!=config_helpers.config_identity(args.baseline_config.resolve()):
                    raise ValueError('prepared kernel config differs from the common baseline')
                config = zephyr.resolved_config((nuttx/'.config').read_text())
                if config.get('CONFIG_USEC_PER_TICK') != str(timer_ms * 1000) or config.get('CONFIG_RR_INTERVAL') != '10':
                    raise ValueError('NuttX timer/timeslice configuration does not match the control')
                # This relink changes only two independent app-selection flags;
                # all kernel/library settings were resolved in the prepared tree.
                # Do not re-resolve them with a host-dependent Kconfig frontend.
                rust=args.platform=='nuttx-rust'
                text=(nuttx/'.config').read_text()
                selected='CONFIG_EXAMPLES_NXRS_STD_APP=y\n' if rust else 'CONFIG_EXAMPLES_NXRS_BENCH=y\n'
                unselected='CONFIG_EXAMPLES_NXRS_BENCH=y\n' if rust else 'CONFIG_EXAMPLES_NXRS_STD_APP=y\n'
                if selected not in text or unselected in text:
                    command(['kconfig-tweak','--enable' if rust else '--disable','CONFIG_EXAMPLES_NXRS_STD_APP'],out,'select-rust.log',nuttx,env)
                    command(['kconfig-tweak','--disable' if rust else '--enable','CONFIG_EXAMPLES_NXRS_BENCH'],out,'select-c.log',nuttx,env)
            finally:
                lock.close()
            if args.platform=='nuttx-c':
                argv=[sys.executable,HERE.parent/'service-footprint/build_c.py',
                    '--nuttx',nuttx,'--apps',apps,'--baseline-config',args.baseline_config,
                    '--source',HERE/'entry.c','--command','es_c','--out',stage,'--reuse-kernel']
                if nuttx_profile == 'minimal':
                    if not (nuttx/'nuttx').is_file(): argv.remove('--reuse-kernel')
                    argv += ['--c-opt-level','2']
                for p in helpers: argv+=['--target-c-source',p]
                for p in headers: argv+=['--target-c-header',p]
                for d in definitions: argv+=['--c-define',d]
                command(argv,out,'build.log',env=env)
                provenance=json.loads((stage/'c-build-provenance.json').read_text())
                prefix='c'
            else:
                argv=[sys.executable,HERE.parent/'service-footprint/relink_rust.py',
                    '--tree',args.nuttx_tree,'--sysroot',args.sysroot,'--out',stage,
                    '--bin','event-services','--command','es_rust','--app-opt-level','2',
                    '--cargo-target',args.nuttx_cargo_target]
                for p in helpers: argv+=['--target-c-source',p]
                for p in headers: argv+=['--target-c-header',p]
                for d in definitions+['ES_RUST=1']: argv+=['--c-define',d]
                if nuttx_profile == 'minimal': argv += ['--c-opt-level','2']
                if getattr(args, 'rust_input_bundle', None):
                    minimal.link_rust_input(args.rust_input_bundle, args.nuttx_tree,
                        helpers, headers, definitions, stage, env, nuttx_helpers,
                        size_kernel=(nuttx_profile == 'minimal'))
                else:
                    command(argv,out,'build.log',env=env)
                provenance=json.loads((stage/'relink-provenance.json').read_text())
                prefix='rust'
            record['native_build']=provenance
            identity=config_helpers.config_identity(stage/'resolved.config')
            if identity!=config_helpers.config_identity(args.baseline_config.resolve()):
                raise ValueError('NuttX kernel config differs from the common baseline')
            record['kernel_config_identity']=identity
            shutil.copy2(stage/(prefix+'.elf'),out/'app.elf')
            # nuttx.bin is already a merged, unpadded boot/partition/app image.
            # Validate its exact prefix against the padded flashing artifact.
            unpadded=(stage/(prefix+'.unpadded.bin')).read_bytes()
            merged=(stage/(prefix+'.merged.bin')).read_bytes()
            if not merged.startswith(unpadded) or any(b!=255 for b in merged[len(unpadded):]):
                raise ValueError('NuttX unpadded image is not the exact merged prefix')
            shutil.copy2(stage/(prefix+'.unpadded.bin'),out/'image.bin')
            shutil.copy2(stage/'resolved.config',out/'resolved.config')
            record['image_header']=image_header(out/'image.bin')
            readelf=args.readelf
        table=command([readelf,'-W','-S',out/'app.elf'],out,'sections.log')
        parsed=parse_sections(table)
        record['elf_sections']=parsed
        record['section_accounting']=section_accounting(parsed)
        if args.platform=='embassy': assert_stack_section(parsed)
        record['image_file']='image.bin';record['elf_file']='app.elf'
        record['artifacts']={p.name:digest(p) for p in out.iterdir() if p.suffix in ('.elf','.bin','.config')}
        record['artifact_bytes']={p.name:p.stat().st_size for p in out.iterdir() if p.name in record['artifacts']}
        if record['source_sha256']!=inventory(nuttx_profile):
            raise ValueError('firmware build inputs changed during compilation')
        record['status']='success';record['failure']=None
    except Exception as exc:
        record['failure']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (out/'build-provenance.json').write_text(json.dumps(record,indent=2)+'\n')
    print('EVENT_SERVICES_BUILD_PASS',args.platform,args.layout,out,flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--platform',required=True,choices=('nuttx-c','nuttx-rust','zephyr-c','embassy'))
    p.add_argument('--layout',required=True,choices=('one','three'))
    p.add_argument('--timer-ms',type=int,choices=(1,10),default=10,
                   help='publication tick resolution; timeslice/stacks stay unchanged')
    p.add_argument('--embassy-scheduling',choices=('event','natural','budget'),default='event')
    p.add_argument('--work-mode',choices=('monolithic','chunked'),default='monolithic')
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--readelf',required=True,type=Path)
    p.add_argument('--espflash',type=Path)
    p.add_argument('--esptool',type=Path,help='NuttX image builder esptool.py (added to PATH)')
    p.add_argument('--cargo-target',type=Path,default=ROOT/'target/event-services-cargo')
    p.add_argument('--nuttx-cargo-target',type=Path,default=ROOT/'target/event-services-nuttx-cargo')
    p.add_argument('--nuttx-profile',choices=('baseline','minimal'),default='baseline')
    p.add_argument('--matched-baseline',type=Path,
                   help='original resolved config used to guard minimal-profile invariants')
    p.add_argument('--rust-input-bundle',type=Path,
                   help='verified frozen compiler input for an app-only final link')
    for name in ('nuttx-tree','sysroot','baseline-config','zephyr','espressif','xtensa','sdk','zephyr-python'):
        p.add_argument('--'+name,type=Path)
    args=p.parse_args()
    required={'embassy':['espflash'],'zephyr-c':['zephyr','espressif','xtensa','sdk','zephyr_python'],
              'nuttx-c':['nuttx_tree','baseline_config'],'nuttx-rust':['nuttx_tree','sysroot','baseline_config']}[args.platform]
    if args.out.exists() or any(getattr(args,n) is None for n in required):
        p.error('fresh output and platform-specific tool/input paths are required')
    if args.nuttx_profile == 'minimal' and (not args.platform.startswith('nuttx-') or not args.matched_baseline):
        p.error('minimal NuttX requires a NuttX platform and --matched-baseline')
    build(args)

if __name__=='__main__': main()
