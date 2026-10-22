"""Actual terminal echo/private-pipe transport with synthetic credentials only."""
import hashlib
import json
import os
from pathlib import Path
import pty
import secrets
import select
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
class BackupKeyTransportTests(unittest.TestCase):
    def test_terminal_key_is_not_echoed_or_passed_in_argv_environment_or_files(self):
        with tempfile.TemporaryDirectory(prefix='nxrs-key-transport-') as temp:
            root=Path(temp).resolve();key=secrets.token_hex(32);witness=root/'witness'
            code=(f'#!{sys.executable}\nimport hashlib,json,os,sys\n'
                  'key=sys.stdin.buffer.read(66)\n'
                  f'expected={hashlib.sha256(key.encode()).hexdigest()!r}\n'
                  'print(json.dumps(dict(private_input=hashlib.sha256(key).hexdigest()==expected,\n'
                  ' argv_clean=all(key.decode() not in value for value in sys.argv),\n'
                  ' env_clean=all(key.decode() not in value for value in os.environ.values()))))\n')
            witness.write_text(code);witness.chmod(0o700)
            pid,fd=pty.fork()
            if pid==0:
                os.execv(sys.executable,[sys.executable,str(ROOT/'tools/activity_backup.py'),str(root/'config.json'),
                                        'backup','--helper',str(witness)])
            output=bytearray();sent=False;deadline=time.monotonic()+8;status=None
            try:
                while time.monotonic()<deadline:
                    if select.select([fd],[],[],.05)[0]:
                        try:chunk=os.read(fd,4096)
                        except OSError:break
                        if not chunk:break
                        output.extend(chunk)
                        if not sent and b'Backup recovery key (64 lowercase hex): ' in output:
                            os.write(fd,(key+'\n').encode());sent=True
                    if status is None:
                        found,value=os.waitpid(pid,os.WNOHANG)
                        if found:status=value
                while status is None and time.monotonic()<deadline:
                    found,value=os.waitpid(pid,os.WNOHANG)
                    if found:status=value
                    else:time.sleep(.01)
                self.assertTrue(sent,'terminal prompt reached before input')
                self.assertIsNotNone(status,'bounded launcher completion')
                self.assertEqual(os.waitstatus_to_exitcode(status),0)
                self.assertNotIn(key.encode(),output,'typed synthetic key must not echo')
                self.assertIn(b'"private_input": true',output)
                self.assertIn(b'"argv_clean": true',output)
                self.assertIn(b'"env_clean": true',output)
                self.assertNotIn(key,witness.read_text())
            finally:
                if status is None:
                    import signal
                    os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
                os.close(fd)

    def test_noninteractive_launcher_refuses_before_requesting_key(self):
        result=subprocess.run([sys.executable,str(ROOT/'tools/activity_backup.py'),'/private/config.json','backup'],
                              input=b'',capture_output=True,timeout=3)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'interactive terminal required',result.stderr)

if __name__=='__main__':unittest.main()
