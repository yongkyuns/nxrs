#!/usr/bin/env python3
"""Interactive key transport only. Rust owns validation, deadlines and maintenance.

Obtain the complete key before spawning the native CLI so human input does not
consume its bounded private-pipe delivery deadline. No key file/argv/env storage.
"""
import argparse
import getpass
import os
from pathlib import Path
import re
import signal
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path);parser.add_argument('operation',choices=['init','backup','check','unlock-stale','restore','resume'])
    parser.add_argument('snapshot',nargs='?');parser.add_argument('--helper',type=Path,default=Path('/usr/local/libexec/nxrs-activity-backup'))
    args=parser.parse_args()
    if not sys.stdin.isatty():parser.error('interactive terminal required; automation uses the native private-pipe interface')
    if not args.helper.is_absolute() or not args.config.is_absolute():parser.error('absolute helper and configuration required')
    if (args.operation in ['restore','resume']) != bool(args.snapshot):parser.error('restore/resume require one full snapshot ID')
    if args.snapshot and not re.fullmatch('[0-9a-f]{64}',args.snapshot):parser.error('full lowercase snapshot ID required')
    secret=getpass.getpass('Backup recovery key (64 lowercase hex): ')
    if not re.fullmatch('[0-9a-f]{64}',secret):parser.error('invalid recovery key shape')
    key=bytearray(secret.encode());secret=None
    try:
        command=[str(args.helper),str(args.config),args.operation]+([args.snapshot] if args.snapshot else [])
        child=subprocess.Popen(command,stdin=subprocess.PIPE,env={'PATH':'/usr/bin:/bin','LANG':'C'})
        try:child.communicate(key)
        except KeyboardInterrupt:
            child.send_signal(signal.SIGTERM)
            # Rust retains the gate until its native child is retired. Do not
            # turn a second local interrupt into abandonment of that ownership.
            while True:
                try:child.wait();break
                except KeyboardInterrupt:child.send_signal(signal.SIGTERM)
        return child.returncode
    finally:
        for index in range(len(key)):key[index]=0
        # Python immutable input/copies and native copies are not proven wiped.

if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,EOFError):raise SystemExit('Backup key input or helper unavailable') from None
