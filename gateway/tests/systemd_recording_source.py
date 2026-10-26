"""Synthetic service-only encoder with a credential pipe to the actual publisher."""
import json,os,signal,subprocess,sys,threading
from pathlib import Path

def run(config):
    value=json.loads(Path(config).read_bytes());token=value['token'].encode()
    if len(token)!=64 or any(ch not in b'0123456789abcdef' for ch in token):raise ValueError('synthetic-token')
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
    environment={'PATH':'/usr/bin:/bin','LANG':'C'};children=[]
    try:
        publication=subprocess.Popen([value['publisher'],value['rtsp'],value['username'],'reference'],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=environment)
        children.append(publication);publication.stdin.write(token);publication.stdin.flush()
        encoder=subprocess.Popen([value['ffmpeg'],'-nostdin','-hide_banner','-loglevel','error','-re',
            '-f','lavfi','-i',"testsrc2=s=320x240:r=15,drawbox=c=gray:t=fill:enable='lt(mod(t,18),4)+gte(mod(t,18),12)'",
            '-an','-c:v','libx264','-threads','2','-preset','ultrafast','-tune','zerolatency',
            '-pix_fmt','yuv420p','-g','15','-bf','0','-f','mpegts','pipe:1'],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=environment)
        children.append(encoder)
        while not stop.is_set():
            data=encoder.stdout.read(8192)
            if not data:raise ValueError('synthetic-source-ended')
            publication.stdin.write(data);publication.stdin.flush()
    finally:
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in reversed(children):
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
        for stream in [getattr(child,name,None) for child in children for name in ('stdin','stdout')]:
            if stream:
                try:stream.close()
                except BrokenPipeError:pass

if __name__=='__main__':
    try:run(sys.argv[1])
    except Exception:sys.exit(1) # Never publish argv, credentials or native output.
