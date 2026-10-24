"""Optional composed host oracle: TLS ingest -> actual recorder -> encrypted store.

Imported only by the real ingest fixture. Uses the existing production binaries,
SDK publisher and Store API. No disk, mount, service or key-custody operation.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from credential_process import assert_boundary, credential_free


def require(value, category):
    if not value: raise AssertionError(category)


def private(path, value):
    with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(value, output)


class Recorder:
    def __init__(self, root, recording, camera_id, tokens, rtsp):
        self.root, self.tokens, self.camera_id = root, tokens, camera_id
        self.worker, self.process = recording['worker'], None
        self.reader = recording.get('reader', self.worker.parent/'nxrs-rtsp-reader')
        self.media_sandbox = recording.get('media_sandbox')
        volume = recording['volume']
        self.storage = dict(directory=str(Path(volume['mount'])/'activity'), camera_ids=[camera_id],
                            retention_seconds=3600, max_bytes=64*1024*1024, max_events=100, protection=volume)
        path = root/'control.json'
        control = json.loads(path.read_bytes())
        control['activity'] = self.storage
        control['recorders'] = [dict(username='recorder_reference', camera_id=camera_id,
            token_sha256=hashlib.sha256(tokens['recorder'].encode()).hexdigest())]
        control['viewers'][0]['manage_recordings'] = True
        path.write_text(json.dumps(control))
        with open(root/'recorder.token', 'x', opener=lambda p, f: os.open(p, f, 0o600)) as output:
            output.write(tokens['recorder'])
        private(root/'recording.json', dict(storage=self.storage, camera_id=camera_id,
            rtsp=f'127.0.0.1:{rtsp}', stream_path='reference', username='recorder_reference',
            token_file=str(root/'recorder.token'), ffmpeg='/usr/bin/ffmpeg', reader=str(self.reader),
            media_sandbox=str(self.media_sandbox) if self.media_sandbox else None, motion_percent=1,
            pre_roll_seconds=2, post_roll_seconds=2, max_clip_seconds=10))
        self.path = '/v1/cameras/'+camera_id+'/activity'

    async def request(self, session, base, path, *, token=None, method='GET', body=None, limit=34*1024*1024):
        token = self.tokens['viewer'] if token is None else token
        for attempt in range(3):
            async with session.request(method, base+path, headers={'Authorization':'Bearer '+token}, json=body) as response:
                data = bytearray()
                async for piece in response.content.iter_chunked(65536):
                    require(len(data)+len(piece)<=limit, 'bounded authenticated recording response')
                    data.extend(piece)
                status = response.status
            if status != 429 or method != 'GET' or attempt == 2: return status, bytes(data)
            await asyncio.sleep(.15*(attempt+1))

    async def start(self, session, base, launch):
        status, data = await self.request(session, base, self.path)
        require(status == 200, 'encrypted recording store available before ingest')
        self.previous = {event['event_id'] for event in json.loads(data)['events']}
        self.process = launch([str(self.worker),str(self.root/'recording.json'),'--require-encrypted-storage',
            *(['--require-media-isolation'] if self.media_sandbox else [])], 'recorder')

    async def observe(self, session, base):
        deadline, inspected, event = asyncio.get_running_loop().time()+45, False, None
        while asyncio.get_running_loop().time()<deadline:
            require(self.process.poll() is None, 'integrated recorder remains running')
            require(credential_free(self.process.pid,self.tokens.values()), 'no recorder credential in parent metadata')
            inspected = bool(assert_boundary(self.process.pid,self.tokens.values())) or inspected
            status, data = await self.request(session,base,self.path)
            require(status == 200, 'encrypted history remains available')
            page = json.loads(data)
            event = next((e for e in page['events'] if e['event_id'] not in self.previous),None)
            if event:break
            await asyncio.sleep(.2)
        require(event is not None and inspected, 'actual TLS-ingested frames produce a new motion clip with observed child metadata')
        path=self.path+'/'+event['event_id']
        status, clip=await self.request(session,base,path+'/clip')
        require(status == 200 and len(clip)==event['clip_bytes'] and len(clip)>1024,
                'authenticated exact encoded clip from encrypted store')
        require((await self.request(session,base,path+'/clip',token=self.tokens['publisher']))[0]==401,
                'publisher cannot read recorded media')
        require((await self.request(session,base,path+'/keep',method='POST',body={'kept':True}))[0]==200,
                'authorized retention of composed recording')
        decoded=await asyncio.create_subprocess_exec('/usr/bin/ffmpeg','-nostdin','-v','error','-i','pipe:0',
            '-f','null','-',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(decoded.communicate(clip),15)
            require(decoded.returncode==0, 'actual composed H264 recording fully decodes')
        finally:
            if decoded.returncode is None:
                decoded.kill();await asyncio.wait_for(decoded.wait(),3)
        frames=(page.get('health') or {}).get('frames_observed',0)
        require(type(frames) is int and frames>=10, 'actual recorder decoded frames before publication')
        print('PASS: real mutual-TLS ingest, native publisher, encrypted recording, role isolation and decoded exact clip',flush=True)
        return dict(schema=1,camera_id=self.camera_id,event_id=event['event_id'],clip_sha256=hashlib.sha256(clip).hexdigest(),
                    clip_bytes=len(clip),frames_observed=frames,encrypted_volume_required=True,decoder_isolation_executed=self.media_sandbox is not None,
                    credential_reader_used=True,
                    camera_tls_front_end_test_only=True,physical_qualified=False)

    async def close(self):
        require(self.process is not None, 'owned recording process required')
        if self.process.poll() is None:self.process.terminate()
        await asyncio.to_thread(self.process.wait,timeout=30)
        require(self.process.returncode==0, 'integrated recorder retires before backup maintenance')
