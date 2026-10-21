#!/usr/bin/env python3
"""Linux hosted-CI-only kernel WireGuard/nginx/Rust/MediaMTX reference, no personal networking."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

spec=importlib.util.spec_from_file_location('vpn',Path(__file__).resolve().parents[1]/'deploy/vpn.py')
vpn=importlib.util.module_from_spec(spec); spec.loader.exec_module(vpn)


def run(gateway, mediamtx):
    if sys.platform!='linux' or os.geteuid()!=0 or os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('isolated hosted Linux runner required')
    names=[]; processes=[]; logs=[]
    print('VPN SETUP: generating private configuration',flush=True)
    def command(args, data=None, timeout=15):
        return subprocess.run([str(a) for a in args],input=data,capture_output=True,check=True,timeout=timeout).stdout
    def inside(ns,*args,**kwargs): return command(['ip','netns','exec',ns,*args],**kwargs)
    def private(path,data):
        with open(path,'xb',opener=lambda p,f:os.open(p,f,0o600)) as stream: stream.write(data)
    with tempfile.TemporaryDirectory(prefix='nxrs-vpn-') as temp:
        parent=Path(temp); root=parent/'deployment'
        suffix=uuid.uuid4().hex[:8]; server='nxrs-s-'+suffix; phone='nxrs-p-'+suffix
        try:
            phone_secret=command(['wg','genkey']); phone_public=command(['wg','pubkey'],phone_secret).decode().strip()
            vpn.generate(root,'10.77.0.1','10.77.0.1','10.77.0.2','192.0.2.1',phone_public)
            # Test changes ONLY certificate/runtime filesystem paths, not the deployed network/proxy policy.
            command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=fixture',
                '-addext','subjectAltName=IP:10.77.0.1','-keyout',root/'key.pem','-out',root/'cert.pem'],timeout=30)
            (root/'nginx.conf').write_text(vpn.nginx_config('10.77.0.1','10.77.0.1',cert=root/'cert.pem',key=root/'key.pem',runtime=root))
            server_private=re.search(r'^PrivateKey = (.+)$',(root/'wg-nxrs.conf').read_text(),re.M).group(1)
            server_public=command(['wg','pubkey'],(server_private+'\n').encode()).decode().strip()
            private(root/'server-wg.conf',command(['wg-quick','strip',root/'wg-nxrs.conf']))
            private(root/'phone-wg.conf',f'[Interface]\nPrivateKey = {phone_secret.decode().strip()}\n[Peer]\nPublicKey = {server_public}\nEndpoint = 192.0.2.1:51820\nAllowedIPs = 10.77.0.1/32\nPersistentKeepalive = 25\n'.encode())
            print('VPN SETUP: creating isolated kernel network namespaces',flush=True)
            for ns in [server,phone]:
                command(['ip','netns','add',ns]); names.append(ns)
                command(['ip','-n',ns,'link','set','lo','up'])
            a='nxa'+suffix; b='nxb'+suffix
            inside(server,'ip','link','add',a,'type','veth','peer','name',b)
            inside(server,'ip','link','set',b,'netns',phone)
            for ns,dev,ip in [(server,a,'192.0.2.1/30'),(phone,b,'192.0.2.2/30')]:
                command(['ip','-n',ns,'link','set',dev,'name','uplink'])
                command(['ip','-n',ns,'addr','add',ip,'dev','uplink'])
                command(['ip','-n',ns,'link','set','uplink','up'])
                inside(ns,'ip','link','add','wg-nxrs','type','wireguard')
                inside(ns,'wg','setconf','wg-nxrs',root/('server-wg.conf' if ns==server else 'phone-wg.conf'))
                inside(ns,'ip','addr','add',('10.77.0.1/32' if ns==server else '10.77.0.2/32'),'dev','wg-nxrs')
                inside(ns,'ip','link','set','wg-nxrs','mtu','1380','up')
            inside(server,'ip','route','add','10.77.0.2/32','dev','wg-nxrs')
            print('VPN SETUP: checking generated firewall and services',flush=True)
            # Foreign table proves applying/reapplying our firewall does not flush unrelated policy.
            inside(server,'nft','add','table','inet','nxrs_sentinel')
            inside(server,'nft','-c','-f',root/'firewall.nft')
            for _ in range(2): inside(server,'nft','-f',root/'firewall.nft')
            inside(server,'nft','list','table','inet','nxrs_sentinel')
            def launch(ns,args,label):
                log=open(root/(label+'.log'),'wb'); os.chmod(log.name,0o600); logs.append(log)
                p=subprocess.Popen(['ip','netns','exec',ns,*map(str,args)],stdout=log,stderr=subprocess.STDOUT)
                processes.append(p); return p
            launch(server,[gateway,root/'config.json'],'control')
            launch(server,[mediamtx,root/'mediamtx.json'],'media')
            inside(server,'nginx','-t','-c',root/'nginx.conf')
            launch(server,['nginx','-c',root/'nginx.conf'],'proxy')
            launch(server,[sys.executable,'-m','http.server','19999','--bind','10.77.0.1'],'denied-listener')
            check='import socket,sys\ntry:\n s=socket.create_connection((sys.argv[1],int(sys.argv[2])),.5); s.close()\nexcept OSError: sys.exit(2)\n'
            def reachable(ns,ip,port):
                result=subprocess.run(['ip','netns','exec',ns,sys.executable,'-c',check,ip,str(port)],capture_output=True,timeout=3)
                if result.returncode not in [0,2]: raise RuntimeError('socket control failed')
                return result.returncode==0
            for _ in range(100):
                if reachable(server,'10.77.0.1',8443) and reachable(server,'10.77.0.1',19999) and reachable(server,'127.0.0.1',8554): break
                time.sleep(.05)
            else: raise RuntimeError('services not ready')
            # Reach the tunnel address over the UNENCRYPTED underlay: binding alone is not a firewall.
            inside(phone,'ip','route','add','10.77.0.1/32','via','192.0.2.1','dev','uplink')
            if reachable(phone,'10.77.0.1',8443): raise RuntimeError('underlay access leaked')
            inside(phone,'ip','route','replace','10.77.0.1/32','dev','wg-nxrs')
            for _ in range(30):
                if reachable(phone,'10.77.0.1',8443): break
                time.sleep(.1)
            else: raise RuntimeError('WireGuard connection failed')
            for port in [19999,18080,18081,8554,8889]:
                if reachable(phone,'10.77.0.1',port): raise RuntimeError('private listener leaked')
            print('VPN BOUNDARY PASS: underlay denied, tunnel HTTPS works, other host ports denied, unrelated nft table retained',flush=True)
            publisher=(root/'publisher.token').read_text().strip()
            launch(server,['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=size=320x240:rate=10',
                '-an','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-profile:v','baseline','-bf','0','-g','10',
                '-pix_fmt','yuv420p','-progress','pipe:1','-f','rtsp','-rtsp_transport','tcp',
                f'rtsp://publisher_reference:{publisher}@127.0.0.1:8554/reference'],'publisher')
            for _ in range(150):
                if any(int(n)>0 for n in re.findall(r'^frame=(\d+)',(root/'publisher.log').read_text(errors='replace'),re.M)): break
                time.sleep(.1)
            else: raise RuntimeError('publisher produced no frames')
            try:
                output=inside(phone,sys.executable,Path(__file__).with_name('vpn_probe.py'),root,timeout=60).decode()
            except subprocess.CalledProcessError as error:
                for line in (error.stdout or b'').decode(errors='replace').splitlines():
                    if re.fullmatch(r'VPN MEDIA FAILED: [a-z ]+; category [A-Za-z]+; HTTP [0-9]+',line):
                        print(line,flush=True)
                raise
            if 'VPN MEDIA PASS:' not in output: raise RuntimeError('receiver failed')
            print('VPN MEDIA PASS: real Rust + nginx + MediaMTX + kernel WireGuard; five decoded H264 frames and DELETE',flush=True)
            handshakes=inside(server,'wg','show','wg-nxrs','latest-handshakes').decode().split()
            if not handshakes or int(handshakes[-1])==0: raise RuntimeError('no authenticated VPN handshake')
            inside(server,'wg','set','wg-nxrs','peer',phone_public,'remove')
            if reachable(phone,'10.77.0.1',8443): raise RuntimeError('removed peer still reachable')
            print('VPN REVOCATION PASS: removed VPN peer cannot reconnect (not application-session revocation)',flush=True)
        finally:
            failures=[]
            for p in reversed(processes):
                try:
                    if p.poll() is None:
                        p.terminate()
                        try: p.wait(timeout=5)
                        except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
                except Exception: failures.append('process')
            for log in logs:
                try: log.close()
                except Exception: failures.append('log')
            for ns in reversed(names):
                try: command(['ip','netns','delete',ns])
                except Exception: failures.append('namespace')
            if failures: raise RuntimeError('fixture cleanup failed')
    print('VPN REFERENCE PASS: private fixtures/namespaces removed; Linux synthetic test, NOT iPhone/cellular',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--gateway',type=Path,required=True); parser.add_argument('--mediamtx',type=Path,required=True)
    args=parser.parse_args()
    try: run(args.gateway.resolve(),args.mediamtx.resolve())
    except Exception:
        print('VPN REFERENCE FAILED (raw commands, credentials and logs suppressed)',flush=True)
        raise SystemExit(1) from None
