#!/usr/bin/env python3
"""Supervise ONLY the supplied controller and activity worker on a local Mac.

Existing MediaMTX, USB supervision, camera firmware, certificates and enrollment
are external. No ports/processes are displaced automatically. Each new child
owns a separate process group, retired before a replacement can start.
"""
import argparse
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import select
import sys
import subprocess
import threading
import time


def atomic(path,value):
    pending=path.with_suffix('.pending')
    with open(pending,'w',opener=lambda p,f:os.open(p,f,0o600)) as file:
        json.dump(value,file); file.write('\n'); file.flush(); os.fsync(file.fileno())
    os.replace(pending,path)


class ExitWatch:
    """Observe an owned child exiting without reaping its reserved group leader."""
    def __init__(self,process):
        self.process=process;self.terminal=False;self.queue=None;self.pending=[]
        if not hasattr(os,'waitid'):
            self.queue=select.kqueue()
            event=select.kevent(process.pid,filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD|select.KQ_EV_ONESHOT,fflags=select.KQ_NOTE_EXIT)
            try:self.pending=self.queue.control([event],1,0)
            except ProcessLookupError:self.terminal=True

    def exited(self):
        if self.terminal:return True
        if self.queue is None:
            self.terminal=os.waitid(os.P_PID,self.process.pid,os.WEXITED|os.WNOWAIT|os.WNOHANG) is not None
        else:
            events=self.pending or self.queue.control(None,1,0);self.pending=[]
            for event in events:
                if event.ident!=self.process.pid or (event.flags&select.KQ_EV_ERROR and event.data!=errno.ESRCH):
                    raise RuntimeError('owned child exit observation refused')
                if not event.flags&select.KQ_EV_ERROR and not event.fflags&select.KQ_NOTE_EXIT:
                    raise RuntimeError('owned child exit observation incomplete')
            self.terminal=bool(events)
        return self.terminal

    def close(self):
        if self.queue is not None:self.queue.close();self.queue=None


def retire(process,watch=None):
    if process.returncode is not None:return # Already retired/reaped by this owner.
    if watch is None:
        # Watch registration failed. The still-unreaped child reserves its PID;
        # stop the whole group before waiting, and never admit a replacement.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5);return
    def send(value):
        try:os.killpg(process.pid,value)
        except ProcessLookupError:pass
        except PermissionError:
            # Darwin can refuse signalling an already exited zombie-only group.
            # Accept only a kernel exit observation while its leader is reserved.
            if sys.platform!='darwin' or not watch.exited():raise
    send(signal.SIGTERM)
    deadline=time.monotonic()+35
    while not watch.exited() and time.monotonic()<deadline:time.sleep(.02)
    send(signal.SIGKILL) # Also retire a crashed parent's surviving descendants.
    deadline=time.monotonic()+5
    while not watch.exited() and time.monotonic()<deadline:time.sleep(.02)
    if not watch.exited():raise RuntimeError('owned child retirement deadline')
    process.wait(timeout=5);watch.close()


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
    children={}; readers={}; watches={}; failed=False; restart={name:0 for name in commands}; due={name:0 for name in commands}
    def drain(pipe,role):
        # Children print fixed diagnostic categories, not credentials; raw lines
        # remain in the private bounded log and never go to stdout.
        try:
            while True:
                line=pipe.readline(1024)
                if not line: return
                log.info('%s: %s',role,line.decode(errors='replace').rstrip())
        finally: pipe.close()
    def finish(role):
        process=children[role];retire(process,watches.get(role));watches.pop(role,None)
        reader=readers.get(role)
        if reader is not None:
            reader.join(timeout=5)
            if reader.is_alive():raise RuntimeError('owned diagnostic reader retirement deadline')
            del readers[role]
        del children[role]
    try:
        while not stop.is_set():
            now=time.monotonic()
            for role,command in commands.items():
                process=children.get(role)
                if process is not None and watches[role].exited():
                    finish(role)
                    restart[role]+=1; due[role]=now+min(8,2**min(3,restart[role]-1))
                    log.info('%s generation retired; restart scheduled',role)
                if role not in children and now>=due[role]:
                    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,start_new_session=True)
                    children[role]=process;watches[role]=ExitWatch(process)
                    reader=threading.Thread(target=drain,args=(process.stderr,role),daemon=True)
                    readers[role]=reader; reader.start()
            state=dict(schema=1,pids={role:p.pid for role,p in children.items()},restarts=restart,
                       supervisor_pid=os.getpid(),updated_at=time.time(),status='running')
            atomic(bundle/'runtime.json',state)
            stop.wait(1)
    except BaseException:
        failed=True
        raise
    finally:
        cleanup_error=None
        for role in reversed(list(children)):
            try:finish(role)
            except BaseException as error:
                failed=True
                if cleanup_error is None:cleanup_error=error
        atomic(bundle/'runtime.json',dict(schema=1,pids={role:p.pid for role,p in children.items()},restarts=restart,
            supervisor_pid=os.getpid(),updated_at=time.time(),status='failed' if failed else 'stopped'))
        if cleanup_error is not None:raise cleanup_error


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,required=True); p.add_argument('--control',type=Path,required=True)
    p.add_argument('--worker',type=Path,required=True)
    run(p.parse_args())
