#!/usr/bin/env python3
"""Owned loopback native HTTP parser and H.264 WebRTC wheel smoke qualification."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from python_environment import installed,require,bounded,Refusal


async def exercise(inventory):
    import aiohttp
    from aiohttp import web
    from aiohttp.http_parser import HttpResponseParser
    import av
    from aiortc import RTCPeerConnection,RTCConfiguration,RTCRtpSender,RTCSessionDescription,VideoStreamTrack
    require(HttpResponseParser.__module__=='aiohttp._http_parser','native-http-parser-required')
    before=installed('media');require(before==inventory,'same-installed-environment')
    class Frames(VideoStreamTrack):
        sequence=0
        async def recv(self):
            pts,time_base=await self.next_timestamp();self.sequence+=1
            frame=av.VideoFrame(128,96,format='yuv420p')
            for index,plane in enumerate(frame.planes):plane.update(bytes([48+self.sequence%96 if index==0 else 128])*plane.buffer_size)
            frame.pts=pts;frame.time_base=time_base;return frame
    sender=RTCPeerConnection(RTCConfiguration(iceServers=[]));receiver=RTCPeerConnection(RTCConfiguration(iceServers=[]))
    track=Frames();transceiver=sender.addTransceiver(track,direction='sendonly')
    h264=[c for c in RTCRtpSender.getCapabilities('video').codecs if c.mimeType=='video/H264']
    require(h264,'native-h264-capability');transceiver.setCodecPreferences(h264)
    decoded=asyncio.get_running_loop().create_future();tasks=set();server=None
    @receiver.on('track')
    def received(remote):
        async def consume():
            try:
                values=[]
                for _ in range(6):
                    frame=await asyncio.wait_for(remote.recv(),5)
                    require((frame.width,frame.height)==(128,96) and type(frame.pts) is int,'actual-decoded-frame')
                    values.append(frame.pts)
                require(all(a<b for a,b in zip(values,values[1:])),'advancing-decoded-frames')
                if not decoded.done():decoded.set_result(len(values))
            except Exception as error:
                if not decoded.done():decoded.set_exception(error)
        task=asyncio.create_task(consume());tasks.add(task)
    async def offer(request):
        value=await request.json()
        await receiver.setRemoteDescription(RTCSessionDescription(sdp=value['sdp'],type=value['type']))
        await receiver.setLocalDescription(await receiver.createAnswer())
        return web.json_response(dict(sdp=receiver.localDescription.sdp,type=receiver.localDescription.type))
    app=web.Application(client_max_size=65536);app.router.add_post('/synthetic-offer',offer)
    runner=web.AppRunner(app,access_log=None)
    try:
        await runner.setup();server=web.TCPSite(runner,'127.0.0.1',0);await server.start()
        port=server._server.sockets[0].getsockname()[1]
        await sender.setLocalDescription(await sender.createOffer())
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),trust_env=False) as client:
            async with client.post('http://127.0.0.1:'+str(port)+'/synthetic-offer',json=dict(sdp=sender.localDescription.sdp,type=sender.localDescription.type)) as response:
                require(response.status==200,'actual-http-response');answer=await response.json()
        await sender.setRemoteDescription(RTCSessionDescription(**answer))
        count=await asyncio.wait_for(decoded,15)
    finally:
        track.stop()
        await asyncio.gather(sender.close(),receiver.close())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await runner.cleanup()
    require(before==installed('media') and all(task.done() for task in tasks)
            and sender.connectionState==receiver.connectionState=='closed','joined-media-cleanup')
    return dict(schema=1,scope='owned-loopback-native-http-and-h264-webrtc-wheels',qualified=True,
                native_http_parser=True,decoded_h264_frames=count,joined_cleanup=True,
                inputs=before['inputs'],native_advisory_closure=False,physical_qualified=False,
                fips_validated=False,deployment_permitted=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--inventory',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    try:
        require(not os.path.lexists(args.report),'new-public-report');raw=bounded(args.inventory,65536)
        report=asyncio.run(asyncio.wait_for(exercise(json.loads(raw)),40))
        report['inventory_sha256']=hashlib.sha256(raw).hexdigest()
        with open(args.report,'x') as output:json.dump(report,output,sort_keys=True,indent=2);output.write('\n')
    except Exception:parser.exit(1,'Python native media loopback qualification refused.\n')
    print('PASS: actual native HTTP parser and six H264 WebRTC frames; peers joined')

if __name__=='__main__':main()
