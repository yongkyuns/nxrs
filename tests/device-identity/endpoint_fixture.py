#!/usr/bin/env python3
"""Actual SDK endpoint lifecycle on owned loopback; only static public JPEGs.

No encoder, forwarding proxy, privileged storage/service or physical provider.
The disposable offline authority never sends its private key to the endpoint.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
from device_tls_camera import SdkCamera,private


def require(value,category):
    if not value:raise AssertionError(category)


def terminal(error):
    if isinstance(error,asyncio.IncompleteReadError):return error.partial==b''
    return isinstance(error,(ConnectionResetError,BrokenPipeError,ssl.SSLError)) and not isinstance(
        error,(ssl.SSLCertVerificationError,ssl.SSLWantReadError,ssl.SSLWantWriteError))


async def exercise(binary):
    os.environ.pop('SSLKEYLOGFILE',None)
    expected=(Path(__file__).with_name('frame.jpg')).read_bytes()
    source=None;writers=[];verified=0
    with tempfile.TemporaryDirectory(prefix='sdk-endpoint-live-') as directory:
        root=Path(directory)
        private(root/'catalog.json',json.dumps(dict(cameras=[dict(camera_id='cam_reference',
            site_id='site_reference',gateway_id='gw_reference',stream_path='reference')],publishers=[])).encode())
        try:
            source=await SdkCamera.start(binary,root,root/'catalog.json',images=(expected,expected))
            initial=await source.command('status');generation=initial['control_generation']
            trust=root/'peer-trust.pem';private(trust,(source.bundle/'ca.pem').read_bytes()+(source.bundle/'revocations.pem').read_bytes())
            context=ssl.create_default_context(cafile=str(trust));context.verify_flags|=ssl.VERIFY_CRL_CHECK_LEAF
            context.minimum_version=context.maximum_version=ssl.TLSVersion.TLSv1_3
            require(context.cert_store_stats()['crl']>=1,'independent peer CRL verification required')
            context.load_cert_chain(source.bundle/('cert-'+source.serials['ingest']+'.pem'),source.keys['ingest'])
            async def connect():
                reader,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',source.port,ssl=context,
                    server_hostname='127.0.0.1',ssl_handshake_timeout=5,limit=8192),6)
                writers.append(writer)
                peer=writer.get_extra_info('ssl_object')
                require(peer.version()=='TLSv1.3' and peer.cipher()[0]=='TLS_CHACHA20_POLY1305_SHA256','actual endpoint TLS profile')
                writer.write(b'GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n');await writer.drain()
                header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
                require(len(header)<=1024 and header.startswith(b'HTTP/1.1 200 OK\r\n'),'actual SDK stream response')
                sequence=0
                async def frame():
                    nonlocal sequence,verified
                    require(await asyncio.wait_for(reader.readline(),3)==b'--nxrs-frame\r\n','SDK multipart boundary')
                    header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
                    require(len(header)<=1024,'bounded SDK part headers')
                    values=dict(line.split(b': ',1) for line in header[:-4].split(b'\r\n'))
                    require(int(values[b'Content-Length'])==len(expected) and values[b'Content-Type']==b'image/jpeg','exact SDK part metadata')
                    current=int(values[b'X-Sequence']);require(current>sequence,'SDK sequence advances');sequence=current
                    require(await asyncio.wait_for(reader.readexactly(len(expected)+2),3)==expected+b'\r\n','actual SDK JPEG payload')
                    verified+=1
                await frame();await frame()
                return reader,frame
            async def closed(reader):
                received=0
                try:
                    while True:
                        data=await asyncio.wait_for(reader.read(4096),3)
                        if not data:return
                        received+=len(data);require(received<=65536,'bounded bytes queued before policy update')
                except Exception as error:
                    if not terminal(error):raise
            reader,frame=await connect()
            for candidate in [b'invalid CRL',source.initial_crl[:-1]+bytes([source.initial_crl[-1]^1])]:
                status=await source.apply_crl(candidate)
                require(status['control_result']=='refused' and status['control_generation']==generation,'malformed or forged CRL cannot change trust')
                await frame()
            status=await source.command('refresh')
            require(status['control_result']=='refreshed' and status['control_refreshes']==1,'actual SDK entropy refresh')
            await frame()
            await source.command('pause')
            status=await source.advance_trust()
            require(status['control_result']=='applied' and status['control_generation']>generation,'signed live CRL update')
            generation=status['control_generation'];await closed(reader)
            await source.command('resume');reader,frame=await connect()
            status=await source.apply_crl(source.initial_crl)
            require(status['control_result']=='refused' and status['control_generation']==generation,'rollback cannot replace accepted trust')
            await frame()
            await source.command('pause');status=await source.advance_trust(revoke=True)
            require(status['control_result']=='applied' and status['control_generation']>generation,'signed peer revocation')
            await closed(reader)
            before=await source.command('status');frames=before['frames']
            refused=False
            try:
                reader,_=await connect()
                raise AssertionError('revoked peer obtained SDK media')
            except Exception as error:
                require(terminal(error),'revoked peer requires terminal native refusal');refused=True
            await asyncio.sleep(.1);after=await source.command('status')
            require(refused and after['frames']==frames and after['connections']>=3 and after['peak']==1,'revoked reconnect emits zero media')
            require(after['control_applied']==2 and after['control_refused']==3 and after['control_refreshes']==1,'all live endpoint controls executed')
            for writer in writers:
                writer.close()
                try:await asyncio.wait_for(writer.wait_closed(),3)
                except Exception as error:
                    if not terminal(error):raise
            writers.clear()
            await source.close();source=None
            return dict(schema=1,scope='actual-host-sdk-endpoint-live-controls',qualified=True,
                camera_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),exact_jpegs_verified=verified,
                signed_updates_applied=2,malformed_forged_rollback_refusals=3,entropy_refreshes=1,
                idle_stream_retired=True,revoked_peer_media_frames=0,reconnect_refused=True,
                joined_cleanup=True,source_peak=after['peak'],native_c_sanitizer_qualified=False,physical_qualified=False,
                protected_sources_qualified=False,device_key_rotation_qualified=False,deployment_permitted=False)
        finally:
            for writer in writers:
                writer.close()
                try:await asyncio.wait_for(writer.wait_closed(),3)
                except Exception:pass
            if source:await source.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--camera',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();os.umask(0o077)
    try:
        result=asyncio.run(exercise(args.camera.resolve()));raw=json.dumps(result,sort_keys=True).encode()
        require(len(raw)<=4096,'bounded public endpoint controls')
        private(args.output,raw)
        print('Actual SDK endpoint live trust/refresh/refusal controls passed; host fixtures only')
    except Exception as error:
        print('SDK endpoint lifecycle controls failed: '+type(error).__name__,file=sys.stderr);return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
