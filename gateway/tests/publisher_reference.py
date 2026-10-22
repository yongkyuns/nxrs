#!/usr/bin/env python3
"""Actual FFmpeg SDK publisher controls with disposable synthetic media/credentials.

Explicit CLI only: no host SDK/media binary means failure, never a skipped proof.
The complete camera/FFmpeg/WebRTC path is separately qualified by ingest_reference.
"""
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

PUBLISHER = None
MEDIAMTX = None


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which('ffmpeg')
        if cls.ffmpeg is None: raise RuntimeError('actual FFmpeg required')
        cls.clips = {}
        for codec in ['libx264','mpeg2video']:
            result = subprocess.run([cls.ffmpeg,'-nostdin','-hide_banner','-loglevel','quiet',
                '-f','lavfi','-i','testsrc2=size=320x240:rate=10','-frames:v','12',
                '-an','-c:v',codec,'-bf','0','-f','mpegts','pipe:1'],capture_output=True,timeout=15)
            if result.returncode or not result.stdout: raise RuntimeError('synthetic MPEG-TS generation failed')
            cls.clips[codec] = result.stdout

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nxrs-publisher-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token = secrets.token_hex(32)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); self.port=sock.getsockname()[1]
        config = dict(logLevel='error',authMethod='internal',authInternalUsers=[
            {'user':'publisher','pass':self.token,'permissions':[dict(action='publish',path='reference')]}],
            api=False,metrics=False,pprof=False,playback=False,rtmp=False,hls=False,srt=False,moq=False,
            rtsp=True,rtspAddress=f'127.0.0.1:{self.port}',rtspTransports=['tcp'],rtspAuthMethods=['basic'],
            webrtc=False,paths=dict(reference=dict(source='publisher',overridePublisher=False)))
        path=self.root/'media.json'
        with open(path,'x',opener=lambda p,f:os.open(p,f,0o600)) as output: json.dump(config,output)
        self.media=subprocess.Popen([str(MEDIAMTX),str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(self.close)
        deadline=time.monotonic()+5
        while True:
            if self.media.poll() is not None: self.fail('actual media fixture failed')
            try:
                with socket.create_connection(('127.0.0.1',self.port),timeout=.2): break
            except OSError:
                if time.monotonic()>=deadline: self.fail('media fixture startup deadline')
                time.sleep(.02)

    def close(self):
        if self.media.poll() is None:
            self.media.terminate()
            try: self.media.wait(3)
            except subprocess.TimeoutExpired: self.media.kill();self.media.wait(3)

    def run_publisher(self,payload,path='reference',stdin=None):
        command=[str(PUBLISHER),f'127.0.0.1:{self.port}','publisher',path]
        # Check the executed command itself, without logging credentials or media.
        self.assertTrue(self.token not in ' '.join(command),'credential in command arguments')
        result=subprocess.run(command,input=payload if stdin is None else None,stdin=stdin,
                              capture_output=True,timeout=8)
        self.assertTrue(self.token.encode() not in result.stdout+result.stderr,'credential in process output')
        return result

    def test_real_sdk_authenticated_publication_and_clean_eof(self):
        result=self.run_publisher(self.token.encode()+self.clips['libx264'])
        self.assertEqual(result.returncode,0,'actual SDK publication must succeed')
        lines=result.stdout.splitlines()
        self.assertTrue(all(re.fullmatch(rb'packet=[0-9]{1,20}',line) for line in lines),'invalid progress envelope')
        counts=[int(line.removeprefix(b'packet=')) for line in lines]
        self.assertGreater(len(counts),0);self.assertEqual(counts,list(range(1,len(counts)+1)))
        self.assertFalse(result.stderr,'unexpected native diagnostic output')

    def test_real_media_rejects_wrong_credential_and_wrong_scope(self):
        for token,path in [(secrets.token_hex(32),'reference'),(self.token,'other')]:
            with self.subTest(path=path):
                result=self.run_publisher(token.encode()+self.clips['libx264'],path)
                self.assertNotEqual(result.returncode,0);self.assertFalse(result.stdout,'unexpected publication progress')
                self.assertTrue(b'Output' in result.stderr,'expected fixed failure category absent')
                self.assertTrue(token.encode() not in result.stderr,'credential in rejected process output')

    def test_missing_truncated_and_invalid_credentials_fail_before_publication(self):
        for payload in [b'',b'a'*63,b'Z'*64,b'\n'*64]:
            with self.subTest(length=len(payload)):
                result=self.run_publisher(payload)
                self.assertNotEqual(result.returncode,0);self.assertFalse(result.stdout,'unexpected publication progress')
                self.assertTrue(b'Credential' in result.stderr,'expected fixed failure category absent')

    def test_actual_sdk_rejects_malformed_media_and_non_h264(self):
        for media in [b'not MPEG-TS',self.clips['mpeg2video']]:
            result=self.run_publisher(self.token.encode()+media)
            self.assertNotEqual(result.returncode,0);self.assertFalse(result.stdout,'unexpected publication progress')
            self.assertTrue(b'Media' in result.stderr,'expected fixed failure category absent')

    def test_regular_file_input_is_refused(self):
        path=self.root/'not-a-pipe'
        with open(path,'xb',opener=lambda p,f:os.open(p,f,0o600)) as output:
            output.write(self.token.encode()+self.clips['libx264'])
        with open(path,'rb') as input:
            result=self.run_publisher(None,stdin=input)
        self.assertNotEqual(result.returncode,0);self.assertTrue(b'Credential' in result.stderr,'expected fixed failure category absent')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publisher',type=Path,required=True)
    parser.add_argument('--mediamtx',type=Path,required=True)
    args=parser.parse_args();PUBLISHER=args.publisher.resolve();MEDIAMTX=args.mediamtx.resolve()
    unittest.main(argv=[__file__],verbosity=2)
