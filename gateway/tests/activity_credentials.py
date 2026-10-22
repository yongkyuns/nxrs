#!/usr/bin/env python3
"""Actual recorder process/pipe/file controls with a non-media child witness.

The real authentication/decoding/clip pipeline is qualified separately by
activity_reference.py. This file never prints raw child output or credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from credential_process import credential_free

BINARY = None


def private(path,data):
    with open(path,'x',opener=lambda p,f:os.open(p,f,0o600)) as output:
        output.write(data if isinstance(data,str) else json.dumps(data))


def alive(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True,timeout=3)
    return result.returncode==0 and bool(result.stdout.strip()) and not result.stdout.lstrip().startswith('Z')


class RecorderProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='nxrs-recorder-controls-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve();self.token=secrets.token_hex(32)
        self.marker=self.root/'children.jsonl';self.worker=None
        self.addCleanup(self.stop)
        private(self.root/'recorder.token',self.token)
        self.ffmpeg=self.root/'child'
        self.witness('hold')
        self.config=dict(storage=dict(directory=str(self.root/'footage'),camera_ids=['cam'],retention_seconds=3600,
                            max_bytes=67108864,max_events=100),camera_id='cam',rtsp='127.0.0.1:8554',
                         stream_path='one',username='recorder',token_file=str(self.root/'recorder.token'),
                         ffmpeg=str(self.ffmpeg),motion_percent=2,pre_roll_seconds=2,post_roll_seconds=4,max_clip_seconds=10)
        self.path=self.root/'config.json';private(self.path,self.config)

    def witness(self,mode):
        # Embed only the digest in the witness, never the token. It hashes only
        # bytes received on stdin; no credential file is opened by this child.
        source=(f'#!{sys.executable}\n'
            'import hashlib,json,os,re,sys,time\n'
            f'mode={mode!r}\n'
            'if mode=="close": os.close(0); data=b""\n'
            'else: data=sys.stdin.buffer.read(513)\n'
            'match=re.search(rb"rtsp://recorder:([0-9a-f]{64})@127\\.0\\.0\\.1:8554/one",data)\n'
            'secret=match.group(1) if match else b""\n'
            f'expected={hashlib.sha256(self.token.encode()).hexdigest()!r}\n'
            'row=dict(pid=os.getpid(),private_input=bool(secret) and hashlib.sha256(secret).hexdigest()==expected,\n'
            '         bounded=len(data)<=512,eof=mode=="hold",\n'
            '         argv_clean=not secret or all(secret.decode() not in value for value in sys.argv),\n'
            '         env_clean=not secret or all(secret.decode() not in value for value in os.environ.values()))\n'
            f'fd=os.open({str(self.marker)!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n'
            'os.write(fd,(json.dumps(row)+"\\n").encode());os.close(fd)\n'
            'if mode=="close": sys.exit(1)\n'
            'time.sleep(120)\n')
        self.ffmpeg.write_text(source);self.ffmpeg.chmod(0o700)

    def rows(self):
        try:return [json.loads(line) for line in self.marker.read_text().splitlines()]
        except (FileNotFoundError,json.JSONDecodeError):return []

    def start(self):
        self.worker=subprocess.Popen([str(BINARY),str(self.path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def stop(self):
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:self.worker.wait(8)
            except subprocess.TimeoutExpired:
                self.worker.kill();self.worker.wait(3);self.fail('recorder shutdown deadline')
        for row in self.rows():
            if alive(row['pid']):
                os.kill(row['pid'],signal.SIGKILL)
                self.fail('recorder left a live witness child')

    def until(self,predicate,seconds=5):
        deadline=time.monotonic()+seconds
        while not predicate():
            if time.monotonic()>=deadline:self.fail('bounded recorder witness did not complete')
            time.sleep(.02)

    def rejected(self,path):
        process=subprocess.Popen([str(BINARY),str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:out,error=process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(3);self.fail('special file must not block recorder startup')
        self.assertNotEqual(process.returncode,0)
        self.assertTrue(self.token.encode() not in out+error,'credential in failure output')
        self.assertFalse(self.marker.exists(),'decoder started before rejecting unsafe state')

    def test_real_metadata_detector_catches_synthetic_argv_and_environment_secrets(self):
        for location in ['clean','argv','environment']:
            with self.subTest(location=location):
                command=[sys.executable,'-c','import time;time.sleep(30)']
                environment={'PATH':'/usr/bin:/bin','LANG':'C'}
                if location=='argv':command.append(self.token)
                if location=='environment':environment['NXRS_SYNTHETIC_TEST_TOKEN']=self.token
                child=subprocess.Popen(command,env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    self.until(lambda:credential_free(child.pid,[self.token]) is not None)
                    self.assertTrue(credential_free(child.pid,[self.token])==(location=='clean'),
                                    'native metadata detector positive/negative control')
                finally:
                    child.kill()
                    try:child.wait(3)
                    except subprocess.TimeoutExpired:
                        raise AssertionError('synthetic metadata control did not retire') from None

    def test_private_stdin_eof_and_cancel_reaps_child(self):
        self.start();self.until(lambda:bool(self.rows()))
        row=self.rows()[0]
        self.assertTrue(all(row[field] for field in ['private_input','bounded','eof','argv_clean','env_clean']),
                        'credential must reach the bounded private pipe only')
        self.assertTrue(alive(row['pid']))
        self.stop();self.assertEqual(self.worker.returncode,0)
        self.assertFalse(alive(row['pid']),'cancellation must reap the child')

    def test_closed_stdin_or_early_exit_never_overlaps_generations(self):
        self.witness('close');self.start();self.until(lambda:len(self.rows())>=3,seconds=7)
        rows=self.rows();self.assertGreaterEqual(len(rows),3)
        self.assertTrue(all(not alive(row['pid']) for row in rows[:-1]),'old children must be reaped before retry')
        self.stop()

    def test_spawn_failure_rolls_back_unpublished_source_directory(self):
        self.config['ffmpeg']=str(self.root/'missing')
        self.path.write_text(json.dumps(self.config));self.start()
        health=self.root/'footage'/'health-cam.json'
        def retired():
            try:return json.loads(health.read_text()).get('reconnects',0)>=1
            except (FileNotFoundError,json.JSONDecodeError):return False
        self.until(retired);self.stop()
        self.assertEqual(list((self.root/'footage'/'spool').iterdir()),[])

    def test_fifo_configuration_is_rejected_without_waiting_for_writer(self):
        fifo=self.root/'config.fifo';os.mkfifo(fifo,0o600);self.rejected(fifo)

    def test_fifo_token_is_rejected_without_waiting_for_writer(self):
        token=self.root/'recorder.token';token.unlink();os.mkfifo(token,0o600);self.rejected(self.path)

    def test_fifo_lifetime_and_store_locks_are_rejected(self):
        for name in ['worker-cam.lock','store.lock']:
            with self.subTest(name=name):
                folder=self.root/('state-'+name);folder.mkdir(mode=0o700)
                os.mkfifo(folder/name,0o600)
                self.config['storage']['directory']=str(folder)
                self.path.write_text(json.dumps(self.config));self.rejected(self.path)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',type=Path,required=True)
    args=parser.parse_args();BINARY=args.worker.resolve(strict=True)
    unittest.main(argv=[__file__],verbosity=2)
