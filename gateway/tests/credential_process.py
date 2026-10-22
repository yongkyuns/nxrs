"""Test-only process metadata inspection; raw argv/environment never leave this module."""
from pathlib import Path
import subprocess
import sys


def descendants(parent):
    result=subprocess.run(['ps','-axo','pid=,ppid='],capture_output=True,text=True,timeout=5)
    if result.returncode: raise RuntimeError('owned process inventory unavailable')
    pairs=[tuple(map(int,line.split())) for line in result.stdout.splitlines() if line.strip()]
    found=set();pending={parent}
    while pending:
        children={pid for pid,ppid in pairs if ppid in pending}-found
        found.update(children);pending=children
    return found


def credential_free(pid, credentials):
    if sys.platform.startswith('linux'):
        try:
            data=[Path(f'/proc/{pid}/{name}').read_bytes() for name in ['cmdline','environ']]
        except FileNotFoundError: return None
    elif sys.platform=='darwin':
        result=subprocess.run(['ps','eww','-p',str(pid),'-o','command='],capture_output=True,timeout=5)
        if result.returncode==1: return None
        if result.returncode: raise RuntimeError('owned process metadata unavailable')
        data=[result.stdout]
    else: raise RuntimeError('qualified process metadata interface required')
    return all(token.encode() not in value for token in credentials for value in data)


def assert_boundary(parent, credentials):
    seen=0
    for pid in descendants(parent):
        status=credential_free(pid,credentials)
        if status is None: continue
        if not status: raise AssertionError('credential in recorder descendant argv/environment')
        seen+=1
    return seen
