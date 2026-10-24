#!/usr/bin/env python3
"""Qualify the actual credential-only RTSP reader using disposable loopback media.

Only synthetic credentials/media are used. Decoder observation requires the Linux
native witness; a host pass without that witness does not prove decoder refusal.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

READER = PUBLISHER = MEDIAMTX = DECODER_GUARD = None


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which('ffmpeg')
        cls.ffprobe = shutil.which('ffprobe')
        if not cls.ffmpeg or not cls.ffprobe:
            raise RuntimeError('actual FFmpeg and ffprobe required')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nxrs-reader-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token = secrets.token_hex(32)
        self.publisher_token = secrets.token_hex(32)
        self.children = []
        self.pump = None
        self.addCleanup(self.close)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        config = dict(logLevel='error', authMethod='internal', authInternalUsers=[
            dict(user='publisher', **{'pass': self.publisher_token}, permissions=[dict(action='publish', path='reference')]),
            dict(user='reader', **{'pass': self.token}, permissions=[dict(action='read', path='reference')])],
            api=False, metrics=False, pprof=False, playback=False, rtmp=False, hls=False, srt=False, moq=False,
            rtsp=True, rtspAddress=f'127.0.0.1:{self.port}', rtspTransports=['tcp'], rtspAuthMethods=['basic'],
            webrtc=False, paths=dict(reference=dict(source='publisher', overridePublisher=False)))
        path = self.root/'media.json'
        with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(config, output)
        media = subprocess.Popen([str(MEDIAMTX), str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(media)
        deadline = time.monotonic()+5
        while True:
            if media.poll() is not None:
                self.fail('actual media fixture failed')
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail('media fixture startup deadline')
                time.sleep(.02)

    def environment(self):
        environment = {'PATH':'/usr/bin:/bin','LANG':'C'}
        if DECODER_GUARD:
            environment['LD_PRELOAD'] = str(DECODER_GUARD)
        return environment

    def publish(self, *, gop=10):
        publisher = subprocess.Popen([str(PUBLISHER), f'127.0.0.1:{self.port}', 'publisher', 'reference'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.environment())
        self.children.append(publisher)
        publisher.stdin.write(self.publisher_token.encode())
        publisher.stdin.flush()
        source = subprocess.Popen([self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'quiet',
            '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10', '-an', '-c:v', 'libx264',
            '-threads', '2', '-preset', 'ultrafast', '-tune', 'zerolatency', '-bf', '0', '-g', str(gop),
            '-f', 'mpegts', '-muxdelay', '0', '-flush_packets', '1', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.children.append(source)
        def copy():
            try:
                while True:
                    data = source.stdout.read(4096)
                    if not data:
                        break
                    publisher.stdin.write(data)
                    publisher.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    publisher.stdin.close()
                except (OSError, ValueError):
                    pass
        self.pump = threading.Thread(target=copy)
        self.pump.start()
        with selectors.DefaultSelector() as ready:
            ready.register(publisher.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(10), 'publisher startup deadline')
            self.assertRegex(publisher.stdout.readline(), rb'^packet=[1-9][0-9]*\n$')
        # Drain ongoing bounded publication status so the source never stalls on it.
        def drain():
            for _ in publisher.stdout:
                pass
        self.drain = threading.Thread(target=drain)
        self.drain.start()

    def close(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(self.children):
            try:
                child.wait(3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(3)
            self.assertNotEqual(child.returncode, 91, 'credentialed helper opened a native decoder')
        for name in ['pump', 'drain']:
            thread = getattr(self, name, None)
            if thread:
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'owned source pipe did not retire')
        for child in self.children:
            for stream in [child.stdin, child.stdout, child.stderr]:
                if stream:
                    stream.close()

    def read(self, credential, user='reader', path='reference', *, stdin=None, active=False):
        command = [str(READER), f'127.0.0.1:{self.port}', user, path]
        self.assertTrue(all(secret not in ' '.join(command) for secret in [self.token, self.publisher_token]))
        child = subprocess.Popen(command, stdin=stdin or subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self.environment())
        self.children.append(child)
        try:
            out, error = child.communicate(credential if stdin is None else None, timeout=4 if active else 8)
        except subprocess.TimeoutExpired:
            child.terminate()
            out, error = child.communicate(timeout=3)
            self.assertTrue(active, 'reader refusal deadline')
        self.assertNotEqual(child.returncode, 91, 'credentialed reader opened a native decoder')
        self.assertLessEqual(len(out), 2*1024*1024)
        self.assertTrue(all(secret.encode() not in out+error for secret in [self.token, self.publisher_token]),
                        'credential in process output')
        return child.returncode, out, error

    def test_authenticated_remux_starts_at_keyframe_and_is_independently_decodable(self):
        self.publish(gop=30)
        # Join between the three-second keyframes used by the 10 FPS SDK fixture.
        time.sleep(1)
        code, data, progress = self.read(self.token.encode(), active=True)
        self.assertTrue(data, f'no compressed output; exit={code}, fixed status={progress if len(progress)<80 else b"oversized"!r}')
        first = subprocess.run([self.ffprobe, '-v', 'quiet', '-show_packets',
            '-read_intervals', '%+#1', '-select_streams', 'v:0', '-show_entries', 'packet=flags',
            '-of', 'json', 'pipe:0'], input=data, capture_output=True, timeout=10)
        self.assertEqual(first.returncode, 0, 'independent first-packet observation required')
        packets = json.loads(first.stdout)['packets']
        self.assertTrue(packets and 'K' in packets[0]['flags'], 'reader must start at a keyframe')
        lines = progress.splitlines()
        self.assertTrue(lines and all(re.fullmatch(rb'packet=[0-9]{1,20}', line) for line in lines), 'fixed packet status required')
        counts = [int(line[7:]) for line in lines]
        self.assertEqual(counts, list(range(1, len(counts)+1)))
        probe = subprocess.run([self.ffprobe, '-v', 'error', '-count_frames', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height,nb_read_frames', '-of', 'json', 'pipe:0'],
            input=data, capture_output=True, timeout=10)
        self.assertEqual(probe.returncode, 0, 'independent compressed-media check failed')
        streams = json.loads(probe.stdout)['streams']
        self.assertEqual(len(streams), 1)
        self.assertEqual((streams[0]['codec_name'], streams[0]['width'], streams[0]['height']), ('h264', 320, 240))
        self.assertGreaterEqual(int(streams[0]['nb_read_frames']), 5)
        decoded = subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-f', 'mpegts',
            '-probesize', '32768', '-analyzeduration', '1000000', '-i', 'pipe:0', '-frames:v', '5',
            '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'], input=data, capture_output=True, timeout=10)
        self.assertEqual(decoded.returncode, 0, 'independent decoder failed')
        self.assertEqual(len(decoded.stdout), 5*320*240)

    def test_wrong_password_role_and_path_release_no_media(self):
        self.publish()
        for credential, user, path in [(secrets.token_hex(32), 'reader', 'reference'),
                (self.publisher_token, 'publisher', 'reference'), (self.token, 'reader', 'other')]:
            with self.subTest(user=user, path=path):
                code, data, error = self.read(credential.encode(), user, path)
                self.assertNotEqual(code, 0)
                self.assertFalse(data, 'unauthorized media output')
                self.assertEqual(error, b'nxrs-rtsp-reader: Media\n')
                self.assertNotIn(credential.encode(), error)

    def test_invalid_truncated_and_missing_credentials(self):
        for value in [b'', b'a'*63, b'Z'*64, b'\n'*64]:
            code, data, error = self.read(value)
            self.assertNotEqual(code, 0)
            self.assertFalse(data)
            self.assertEqual(error, b'nxrs-rtsp-reader: Credential\n')

    def test_regular_file_is_refused(self):
        path = self.root/'regular'
        path.write_bytes(self.token.encode())
        with open(path, 'rb') as source:
            code, data, error = self.read(None, stdin=source)
        self.assertNotEqual(code, 0)
        self.assertFalse(data)
        self.assertEqual(error, b'nxrs-rtsp-reader: Credential\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader', type=Path, required=True)
    parser.add_argument('--publisher', type=Path, required=True)
    parser.add_argument('--mediamtx', type=Path, required=True)
    parser.add_argument('--decoder-guard', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    READER, PUBLISHER, MEDIAMTX = [p.resolve(strict=True) for p in [args.reader, args.publisher, args.mediamtx]]
    if args.decoder_guard:
        if not sys.platform.startswith('linux'):
            parser.exit(1, 'Linux native decoder observer required.\n')
        DECODER_GUARD = args.decoder_guard.resolve(strict=True)
        control = DECODER_GUARD.parent/'decoder-control'
        plain = subprocess.run([str(control)],env={'PATH':'/usr/bin:/bin','LANG':'C'},capture_output=True,timeout=5)
        guarded = subprocess.run([str(control)],env={'PATH':'/usr/bin:/bin','LANG':'C','LD_PRELOAD':str(DECODER_GUARD)},capture_output=True,timeout=5)
        if plain.returncode != 0 or guarded.returncode != 91:
            parser.exit(1, 'Native decoder observer positive control failed.\n')
    result = unittest.main(argv=[__file__], verbosity=2, exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
    if args.report:
        repository=Path(__file__).resolve().parents[2]
        names={'gateway/Cargo.toml','gateway/Cargo.lock','.github/workflows/monitoring-ingest.yml',
            'gateway/tests/reader_reference.py','gateway/tests/build_publisher_witness.py',
            'gateway/tests/native/publisher_no_decoder.c','gateway/tests/native/publisher_decoder_control.c'}
        for crate in ['ingest','media-process','media-sandbox','private-file']:
            names.add('gateway/'+crate+'/Cargo.toml')
            names.update(str(p.relative_to(repository)) for p in (repository/'gateway'/crate/'src').rglob('*.rs'))
        executables=dict(reader=READER,publisher=PUBLISHER,mediamtx=MEDIAMTX,
            ffmpeg=Path(ReaderTests.ffmpeg),ffprobe=Path(ReaderTests.ffprobe))
        if DECODER_GUARD:
            executables.update(decoder_guard=DECODER_GUARD,decoder_control=control)
        report=dict(checkout=dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repository,text=True,timeout=10).strip(),
            clean=not bool(subprocess.check_output(['git','status','--porcelain'],cwd=repository,timeout=10).strip())),
            schema=1,scope='actual-host-rtsp-reader-remux-and-credential-controls',qualified=True,
            native_decoder_open_observer_executed=DECODER_GUARD is not None,
            independent_decoded_frames=5,credential_refusal_cases=8,tests_run=result.testsRun,
            inputs={n:hashlib.sha256((repository/n).read_bytes()).hexdigest() for n in sorted(names)},
            executables={n:hashlib.sha256(p.read_bytes()).hexdigest() for n,p in executables.items()},
            physical_qualified=False,device_tls_runtime_qualified=False,fips_validated=False,deployment_permitted=False)
        if result.testsRun != 4:
            parser.exit(1,'Complete reader controls required for evidence.\n')
        with args.report.open('x') as output:
            json.dump(report,output,sort_keys=True,indent=2);output.write('\n')
