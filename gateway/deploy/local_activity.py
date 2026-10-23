#!/usr/bin/env python3
"""Supervise ONLY the supplied controller and activity worker on a local Mac.

Existing MediaMTX, USB supervision, camera firmware, certificates and enrollment
are external. No ports/processes are displaced automatically. Each new child
owns a separate process group, retired before a replacement can start.
"""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def atomic(path,value):
    pending=path.with_suffix('.pending')
    with open(pending,'w',opener=lambda p,f:os.open(p,f,0o600)) as file:
        json.dump(value,file); file.write('\n'); file.flush(); os.fsync(file.fileno())
    os.replace(pending,path)


def retire(process):
    # A crashed Rust parent can leave an FFmpeg child. Its process group is still
    # ours even after the leader exits; never launch another generation first.
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=35)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=5)
    try: os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError: pass


def run(args):
    os.umask(0o077)
    bundle=args.bundle.resolve()
    if not bundle.is_dir() or bundle.stat().st_mode&0o077:
        raise ValueError('private activity bundle required')
    log=logging.getLogger('nxrs-local-activity'); log.setLevel(logging.INFO)
    handler=RotatingFileHandler(bundle/'supervision.log',maxBytes=1048576,backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s')); log.addHandler(handler)
    stop=threading.Event()
    def signal_stop(*_): stop.set()
    signal.signal(signal.SIGTERM,signal_stop); signal.signal(signal.SIGINT,signal_stop)
    commands={'control':[str(args.control.resolve()),str(bundle/'control.json')],
              'activity':[str(args.worker.resolve()),str(bundle/'activity.json')]}
    children={}; readers={}; restart={name:0 for name in commands}; due={name:0 for name in commands}
    def drain(pipe,role):
        # Children print fixed diagnostic categories, not credentials; raw lines
        # remain in the private bounded log and never go to stdout.
        try:
            while True:
                line=pipe.readline(1024)
                if not line: return
                log.info('%s: %s',role,line.decode(errors='replace').rstrip())
        finally: pipe.close()
    try:
        while not stop.is_set():
            now=time.monotonic()
            for role,command in commands.items():
                process=children.get(role)
                if process is not None and process.poll() is not None:
                    retire(process)
                    readers.pop(role).join(timeout=5)
                    del children[role]
                    restart[role]+=1; due[role]=now+min(8,2**min(3,restart[role]-1))
                    log.info('%s generation retired; restart scheduled',role)
                if role not in children and now>=due[role]:
                    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,start_new_session=True)
                    children[role]=process
                    reader=threading.Thread(target=drain,args=(process.stderr,role),daemon=True)
                    readers[role]=reader; reader.start()
            state=dict(schema=1,pids={role:p.pid for role,p in children.items()},restarts=restart,
                       supervisor_pid=os.getpid(),updated_at=time.time(),status='running')
            atomic(bundle/'runtime.json',state)
            stop.wait(1)
    finally:
        for process in reversed(list(children.values())): retire(process)
        for reader in readers.values(): reader.join(timeout=5)
        atomic(bundle/'runtime.json',dict(schema=1,pids={},restarts=restart,supervisor_pid=os.getpid(),
            updated_at=time.time(),status='stopped'))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,required=True); p.add_argument('--control',type=Path,required=True)
    p.add_argument('--worker',type=Path,required=True)
    run(p.parse_args())
