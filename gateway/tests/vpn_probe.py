#!/usr/bin/env python3
"""Synthetic receiver executed inside the isolated WireGuard peer namespace."""
import argparse
import asyncio
import json
from pathlib import Path
import ssl
from urllib.parse import urljoin, urlsplit
import aiohttp
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, RTCRtpSender


def require(ok, name):
    if not ok: raise AssertionError(name)


async def run(root):
    base='https://10.77.0.1:8443'
    tls=ssl.create_default_context(cafile=str(root/'cert.pem'))
    viewer=(root/'viewer.token').read_text().strip()
    peer=RTCPeerConnection(RTCConfiguration(iceServers=[]))
    track_ready=asyncio.get_running_loop().create_future()
    @peer.on('track')
    def track(value):
        if value.kind=='video' and not track_ready.done(): track_ready.set_result(value)
    video=peer.addTransceiver('video',direction='recvonly')
    video.setCodecPreferences([c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType.lower()=='video/h264'])
    session=None; token=None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        async def request(method, path, credential=None, **kwargs):
            headers=kwargs.pop('headers',{})
            if credential: headers['Authorization']='Bearer '+credential
            async with http.request(method,base+path,headers=headers,ssl=tls,allow_redirects=False,**kwargs) as response:
                require(response.headers.get('Cache-Control')=='no-store','cache boundary')
                return response.status,await response.read(),response.headers.get('Location')
        try:
            require((await request('GET','/v1/cameras'))[0]==401,'VPN membership must not authenticate a viewer')
            status,data,_=await request('GET','/v1/cameras',viewer)
            require(status==200 and json.loads(data)['cameras'][0]['source']=='synthetic','authorized catalog')
            for path in ['/internal/mediamtx/auth','/internal/viewers/viewer_reference/revoke','/reference/whip','/v3/config/global/get']:
                require((await request('POST',path,viewer,data=b'{}'))[0]==404,'private route blocked')
            await peer.setLocalDescription(await peer.createOffer())
            status,data,_=await request('POST','/v1/cameras/cam_reference/playback-grants',viewer,json={})
            require(status==201,'grant creation')
            grant=json.loads(data); token=grant['access_token']
            require(grant['endpoint']==base+'/reference/whep','external HTTPS grant origin')
            status,_,_=await request('POST','/reference/whep',viewer,data=peer.localDescription.sdp,headers={'Content-Type':'application/sdp'})
            require(status==401,'account credential cannot read media')
            status,answer,location=await request('POST','/reference/whep',token,data=peer.localDescription.sdp,headers={'Content-Type':'application/sdp'})
            require(status==201 and location,'WHEP admission')
            url=urlsplit(urljoin(base+'/reference/whep',location))
            require(url.scheme=='https' and url.netloc=='10.77.0.1:8443' and not url.query,'same-origin session')
            session=url.path
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer.decode(),type='answer'))
            receiver=await asyncio.wait_for(track_ready,15)
            frames=[await asyncio.wait_for(receiver.recv(),15) for _ in range(5)]
            require(all((f.width,f.height)==(320,240) for f in frames),'decoded dimensions')
            require(len({f.pts for f in frames})==5,'advancing frames')
            await peer.close()
            require((await request('DELETE',session,token))[0] in [200,204],'session cleanup')
            session=None
            print('VPN MEDIA PASS: 5 advancing H264 frames; scoped API/media auth; HTTPS proxy; WHEP DELETE',flush=True)
        finally:
            await peer.close()
            if session: await request('DELETE',session,token)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('directory',type=Path); args=parser.parse_args()
    try: asyncio.run(run(args.directory))
    except Exception:
        print('VPN MEDIA FAILED (details suppressed to protect credentials and SDP)',flush=True)
        raise SystemExit(1) from None
