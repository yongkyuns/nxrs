#!/usr/bin/env python3
"""Actual recorder process/pipe/file controls with a non-media child witness.

The real authentication/decoding/clip pipeline is qualified separately by
activity_reference.py. This file never prints raw child output or credentials.
"""
import argparse
import fcntl
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
        self.ffmpeg=self.root/'decoder'
        self.reader=self.root/'reader'
        self.witness('hold')
        self.config=dict(storage=dict(directory=str(self.root/'footage'),camera_ids=['cam'],retention_seconds=3600,
                            max_bytes=67108864,max_events=100),camera_id='cam',rtsp='127.0.0.1:8554',
                         stream_path='one',username='recorder',token_file=str(self.root/'recorder.token'),
                         ffmpeg=str(self.ffmpeg),reader=str(self.reader),motion_percent=2,pre_roll_seconds=2,post_roll_seconds=4,max_clip_seconds=10)
        self.path=self.root/'config.json';private(self.path,self.config)

    def witness(self,mode):
        # Only the reader's inherited pipe receives the synthetic token. The
        # decoder observes compressed-media bytes through an independent pipe.
        for role, path in [('reader', self.reader), ('decoder', self.ffmpeg)]:
            source=(f'#!{sys.executable}\n'
                'import hashlib,json,os,sys,time\n'
                f'mode={mode!r}; role={role!r}\n'
                'if mode=="close": os.close(0); data=b""\n'
                'elif role=="reader": data=sys.stdin.buffer.read(65)\n'
                'else: data=sys.stdin.buffer.read(10)\n'
                f'expected={hashlib.sha256(self.token.encode()).hexdigest()!r}\n'
                'secret=data if role=="reader" else b""\n'
                'row=dict(pid=os.getpid(),role=role,private_input=bool(secret) and hashlib.sha256(secret).hexdigest()==expected,\n'
                '         bounded=len(data)==(64 if role=="reader" else 10),eof=role=="reader" and mode=="hold",\n'
                '         media_only=role=="decoder" and data==b"media-only",\n'
                '         argv_clean=not secret or all(secret.decode() not in value for value in sys.argv),\n'
                '         env_clean=not secret or all(secret.decode() not in value for value in os.environ.values()))\n'
                f'fd=os.open({str(self.marker)!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n'
                'os.write(fd,(json.dumps(row)+"\\n").encode());os.close(fd)\n'
                'if mode=="close": sys.exit(1)\n'
                'if role=="reader":\n'
                ' sys.stdout.buffer.write(b"media-only");sys.stdout.flush()\n'
                ' sys.stderr.write("packet=1\\n");sys.stderr.flush()\n'
                'if mode=="exit-"+role: sys.exit(1)\n'
                'if role=="decoder" and mode=="segment-refusal":\n'
                ' sys.stdout.write("part-000000.ts,0,11\\n");sys.stdout.flush()\n'
                'time.sleep(120)\n')
            path.write_text(source);path.chmod(0o700)

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

    def test_segment_refusal_is_redacted_and_retires_both_children_before_readmission(self):
        self.witness('segment-refusal')
        path = self.root/'worker.log'
        with path.open('wb') as log:
            self.worker = subprocess.Popen([str(BINARY), str(self.path)],
                stdout=subprocess.DEVNULL, stderr=log)
            self.until(lambda:len(self.rows()) >= 4)
            first = self.rows()[:2]
            self.assertTrue(all(not alive(row['pid']) for row in first),
                            'refused segment must retire both children before readmission')
            self.stop()
        raw = path.read_bytes()
        self.assertIn(b'nxrs-camera-activity: segment interval too long\n', raw)
        self.assertNotIn(self.token.encode(), raw)
        self.assertNotIn(b'part-000000.ts', raw)

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
        self.start();self.until(lambda:len(self.rows())==2)
        reader=next(row for row in self.rows() if row['role']=='reader')
        decoder=next(row for row in self.rows() if row['role']=='decoder')
        self.assertTrue(all(reader[field] for field in ['private_input','bounded','eof','argv_clean','env_clean']),
                        'credential must reach only the reader private pipe')
        self.assertTrue(decoder['media_only'] and decoder['bounded'] and not decoder['private_input'],
                        'decoder must receive compressed media without the credential preamble')
        self.assertTrue(all(alive(row['pid']) for row in self.rows()))
        self.stop();self.assertEqual(self.worker.returncode,0)
        self.assertTrue(all(not alive(row['pid']) for row in self.rows()),'cancellation must reap both children')

    def test_closed_stdin_or_early_exit_never_overlaps_generations(self):
        self.witness('close');self.start();self.until(lambda:len(self.rows())>=4,seconds=7)
        rows=self.rows();self.assertGreaterEqual(len(rows),3)
        self.assertTrue(all(not alive(row['pid']) for row in rows[:-2]),'old children must be reaped before retry')
        self.stop()

    def test_either_child_failure_reaps_its_counterpart_before_retry(self):
        for role in ['reader','decoder']:
            with self.subTest(role=role):
                self.marker.unlink(missing_ok=True);self.witness('exit-'+role)
                self.start();self.until(lambda:len(self.rows())>=4,seconds=7)
                self.assertTrue(all(not alive(row['pid']) for row in self.rows()[:-2]),
                                'failed source/decoder must retire both old children before retry')
                self.stop()

    def test_stalled_reader_status_retires_both_children_before_recovery(self):
        self.start();self.until(lambda:len(self.rows())>=4,seconds=12)
        self.assertTrue(all(not alive(row['pid']) for row in self.rows()[:-2]))
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

    def test_strict_storage_refuses_a_plain_configuration_before_child_or_directory(self):
        process=subprocess.run([str(BINARY),str(self.path),'--require-encrypted-storage'],
                               capture_output=True,timeout=5)
        self.assertNotEqual(process.returncode,0)
        self.assertIn(b'encrypted activity storage unavailable',process.stderr)
        self.assertFalse((self.root/'footage').exists())
        self.assertFalse(self.marker.exists())

    def test_invalid_token_is_refused_before_child_or_storage(self):
        for value in ['a'*63,'a'*65,'A'*64,'a'*63+'!']:
            (self.root/'recorder.token').write_text(value)
            self.rejected(self.path)
            self.assertFalse((self.root/'footage').exists())

    def test_required_isolation_refuses_missing_configuration_before_storage_or_child(self):
        process=subprocess.run([str(BINARY),str(self.path),'--require-media-isolation'],capture_output=True,timeout=5)
        self.assertNotEqual(process.returncode,0)
        self.assertFalse((self.root/'footage').exists())
        self.assertFalse(self.marker.exists())

    def test_failed_installed_isolation_probe_cannot_fall_back(self):
        self.config['media_sandbox']=str(self.root/'missing-launcher')
        self.path.write_text(json.dumps(self.config))
        self.rejected(self.path)
        self.assertFalse((self.root/'footage').exists())

    def test_reader_spawn_failure_reaps_already_started_decoder(self):
        self.config['reader']=str(self.root/'missing-reader')
        self.path.write_text(json.dumps(self.config));self.start()
        health=self.root/'footage'/'health-cam.json'
        def retired():
            try:return json.loads(health.read_text()).get('reconnects',0)>=1
            except (FileNotFoundError,json.JSONDecodeError):return False
        self.until(retired);self.stop()
        self.assertEqual(list((self.root/'footage'/'spool').iterdir()),[])

    def store_lock(self):
        root=self.root/'footage';root.mkdir(mode=0o700)
        for name in ['events','staging','spool']:(root/name).mkdir(mode=0o700)
        lock=open(root/'store.lock','x+b',opener=lambda p,f:os.open(p,f,0o600))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.addCleanup(lock.close)
        return lock

    def test_transient_store_contention_recovers_before_any_source_admission(self):
        lock=self.store_lock();self.start();time.sleep(.2)
        self.assertIsNone(self.worker.poll(),'transient controller lock must not abort recorder startup')
        self.assertFalse(self.marker.exists(),'no children before storage admission')
        fcntl.flock(lock,fcntl.LOCK_UN)
        self.until(lambda:len(self.rows())==2)
        self.stop();self.assertEqual(self.worker.returncode,0)

    def test_startup_store_contention_has_a_finite_deadline(self):
        self.store_lock()
        process=subprocess.run([str(BINARY),str(self.path)],capture_output=True,timeout=4)
        self.assertNotEqual(process.returncode,0)
        self.assertEqual(process.stderr,b'nxrs-camera-activity: activity storage busy\n')
        self.assertFalse(self.marker.exists(),'exhausted admission cannot start children')

    def test_cancellation_during_store_admission_is_clean_and_starts_no_child(self):
        self.store_lock();self.start();time.sleep(.2)
        self.assertIsNone(self.worker.poll(),'admission must remain cancellable')
        self.stop();self.assertEqual(self.worker.returncode,0)
        self.assertFalse(self.marker.exists())

    def test_startup_retry_does_not_bypass_an_existing_camera_worker_lease(self):
        self.start();self.until(lambda:len(self.rows())==2)
        second=subprocess.run([str(BINARY),str(self.path)],capture_output=True,timeout=3)
        self.assertNotEqual(second.returncode,0)
        self.assertEqual(second.stderr,b'nxrs-camera-activity: activity storage busy\n')
        self.assertEqual(len(self.rows()),2,'only one admitted reader/decoder pair')
        self.stop()

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
