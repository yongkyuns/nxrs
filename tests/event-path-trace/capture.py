#!/usr/bin/env python3
"""Capture/replay one P4 command after a fresh boot. Never flash or retry."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import termios
import time

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis = load('p4_analysis', HERE/'analyze.py')
builder = load('p4_builder', HERE/'build.py')
# Reuse P2/P0 bounded console I/O read-only; no shared module substitution.
p2 = load('p4_p2_capture', HERE.parent/'event-path-perf/capture.py')
require = analysis.require


def read_json(path):
    return json.loads(p2.bounded_bytes(path,8*1024*1024), object_pairs_hook=analysis.unique)


def receipt(directory):
    record = read_json(p2.checked_file(directory,'build.json'))
    require(record.get('schema')=='event-path-trace-build-v1' and record.get('status')=='complete' and
            record.get('failure') is None and record.get('mode') in {'baseline','trace'}, 'incomplete P4 build')
    require(builder.canonical({k:record[k] for k in builder.IDENTITY}) == record.get('build_id'), 'wrong frozen build identity')
    pins = read_json(HERE/'native-pins.json')
    require(record.get('native_source_sha256') == pins['files'], 'wrong native source pins')
    for key in ('kernel_revision','apps_revision'): require(record.get(key)==pins[key], 'wrong native revision')
    artifacts = record.get('artifacts_sha256')
    require(isinstance(artifacts,dict) and 1 <= len(artifacts)<=64, 'invalid artifact inventory')
    total = 0
    for name, expected in artifacts.items():
        p2.portable_path(name)
        path = p2.checked_file(directory,name)
        total += path.stat().st_size
        require(total<=256*1024*1024 and path.stat().st_size<=128*1024*1024, 'oversized build artifacts')
        require(p2.digest(path)==expected, 'changed artifact: '+name)
    for key in ('image','elf','config'):
        name = record[key+'_file']
        require(artifacts.get(name)==record[key+'_sha256'], 'unbound '+key)
    for name,expected in pins['files'].items():
        require(artifacts.get('native-sources/'+name)==expected, 'unbound native source')
    hook = read_json(p2.checked_file(directory,'prepared/std-channel-hook.json'))
    require(hook.get('channel_instrumented') is False and hook.get('hook_stage')==0 and
            hook['selected']['sha256']==hook['original']['sha256']==record['std_array_sha256'], 'wrong std source')
    require(read_json(p2.checked_file(directory,'crossbeam-source.json'))==record['crossbeam'], 'wrong registry receipt')
    require(record.get('ready_wrapper_references',0)>0, 'missing ready wrapper')
    builder.p2.boot_image.verify(directory,record)
    config = builder.p2.baseline.config_values(p2.checked_file(directory,record['config_file']))
    for name in ('SCHED_INSTRUMENTATION','SCHED_INSTRUMENTATION_SWITCH','SCHED_INSTRUMENTATION_IRQHANDLER','DRIVERS_NOTE'):
        require((config.get('CONFIG_'+name)=='y') == (record['mode']=='trace'), 'wrong instrumentation configuration')
    for name in builder.DISABLED:
        require(config.get('CONFIG_'+name) != 'y', 'unexpected note path')
    return record


def arguments(args):
    require(args.backend in {'std','messaging','crossbeam'} and args.policy in {'equal','unequal'}, 'invalid case')
    require(args.recording in {'quiet','record'}, 'invalid recording arm')
    analysis.integer(args.samples,1,128); analysis.integer(args.warmup,0,64)
    return f'event_path_trace {args.backend} {args.policy} {args.recording} {args.samples} {args.warmup}'


def replay(directory, build_directory):
    build = receipt(build_directory)
    run = read_json(p2.checked_file(directory,'run.json'))
    require(run.get('schema')=='event-path-trace-capture-v1' and run.get('failure') is None and
            run.get('fresh_boot_observed') is True and run.get('build_id')==build['build_id'], 'failed/incomplete capture')
    console = p2.bounded_bytes(p2.checked_file(directory,'console.log'),2*1024*1024)
    require(hashlib.sha256(console).hexdigest()==run.get('console_sha256'), 'changed console')
    session = p2.bounded_bytes(p2.checked_file(directory,'session.json'),64*1024)
    require(hashlib.sha256(session).hexdigest()==run.get('session_sha256'), 'changed session')
    session = json.loads(session,object_pairs_hook=analysis.unique)
    require(session.get('schema')=='event-path-trace-session-v1' and
            session.get('fresh_backup_verified') is True and session.get('flash_verified') is True and
            session.get('image_sha256')==build['image_sha256'], 'missing physical preparation proof')
    require(p2.console.fresh_boot(console) and console.rstrip().endswith(b'nsh>'), 'missing boot/final prompt')
    return analysis.analyze(console.decode(errors='strict'),build,backend=run['backend'],policy=run['policy'],
        recording=run['recording']=='record',samples=run['samples'],warmup=run['warmup'])


def capture(args):
    build = receipt(args.build_directory)
    command = arguments(args)
    require(args.recording=='quiet' or build['mode']=='trace', 'baseline refuses recording')
    session_bytes = p2.bounded_bytes(args.session,64*1024)
    session = json.loads(session_bytes,object_pairs_hook=analysis.unique)
    require(session.get('schema')=='event-path-trace-session-v1' and session.get('fresh_backup_verified') is True and
            session.get('flash_verified') is True and session.get('image_sha256')==build['image_sha256'], 'invalid physical session')
    require(not args.output.exists(), 'preserve previous runs; output must be new')
    args.output.mkdir(parents=True)
    (args.output/'session.json').write_bytes(session_bytes)
    run = {'schema':'event-path-trace-capture-v1','build_id':build['build_id'],'backend':args.backend,
           'policy':args.policy,'recording':args.recording,'samples':args.samples,'warmup':args.warmup,
           'command':command,'fresh_boot_observed':False,'failure':'incomplete capture',
           'session_sha256':hashlib.sha256(session_bytes).hexdigest()}
    transcript = bytearray()
    fd = saved = None
    try:
        fd,saved = p2.console.serial_open(args.port)
        print('P4 console open: reset the board now (one command per fresh boot).',flush=True)
        deadline = time.monotonic()+args.timeout
        p2.console.read_until(fd,transcript,p2.console.fresh_boot,deadline)
        run['fresh_boot_observed']=True
        require(not analysis.FAULT.search(transcript.decode(errors='replace')), 'boot fault/warning')
        p2.console.write_command(fd,command,deadline)
        p2.console.read_until(fd,transcript,lambda data: b'"record":"result"' in data and data.rstrip().endswith(b'nsh>'),deadline)
        # Preserve late failures after the final prompt, just as the P2 runner.
        try:
            p2.console.read_until(fd,transcript,lambda _:False,min(deadline,time.monotonic()+1))
        except TimeoutError:
            pass
        analysis.analyze(transcript.decode(errors='strict'),build,backend=args.backend,policy=args.policy,
                         recording=args.recording=='record',samples=args.samples,warmup=args.warmup)
        run['failure']=None
    except BaseException as error:
        run['failure']=f'{type(error).__name__}: {error}'
        raise
    finally:
        if fd is not None:
            try:
                if saved is not None: termios.tcsetattr(fd,termios.TCSANOW,saved)
            except OSError as error:
                run['cleanup_failure']=str(error)
                run['failure']=run['failure'] or 'serial restoration failed'
            finally:
                os.close(fd)
        (args.output/'console.log').write_bytes(transcript)
        run['console_sha256']=hashlib.sha256(transcript).hexdigest()
        (args.output/'run.json').write_text(json.dumps(run,indent=2)+'\n')
    require(run['failure'] is None, 'capture cleanup failed')
    summary = replay(args.output,args.build_directory)
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation',required=True)
    collect = sub.add_parser('serial')
    collect.add_argument('--port',required=True)
    collect.add_argument('--session',type=Path,required=True)
    collect.add_argument('--output',type=Path,required=True)
    collect.add_argument('--backend',choices=['std','messaging','crossbeam'],required=True)
    collect.add_argument('--policy',choices=['equal','unequal'],required=True)
    collect.add_argument('--recording',choices=['quiet','record'],required=True)
    collect.add_argument('--samples',type=int,default=64)
    collect.add_argument('--warmup',type=int,default=16)
    collect.add_argument('--timeout',type=int,default=120,choices=range(60,301))
    check = sub.add_parser('replay'); check.add_argument('directory',type=Path)
    for command_parser in (collect,check): command_parser.add_argument('--build-directory',type=Path,required=True)
    args = parser.parse_args()
    if args.operation=='serial': capture(args)
    else: print(json.dumps(replay(args.directory,args.build_directory),indent=2))
