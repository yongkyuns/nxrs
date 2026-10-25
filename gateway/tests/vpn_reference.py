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
from vpn_progress import read_progress

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
            source_process=launch(server,['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=size=320x240:rate=10',
                '-an','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-profile:v','baseline','-bf','0','-g','10',
                '-pix_fmt','yuv420p','-progress','pipe:1','-f','rtsp','-rtsp_transport','tcp',
                f'rtsp://publisher_reference:{publisher}@127.0.0.1:8554/reference'],'publisher')
            for _ in range(150):
                if any(int(n)>0 for n in re.findall(r'^frame=(\d+)',(root/'publisher.log').read_text(errors='replace'),re.M)): break
                time.sleep(.1)
            else: raise RuntimeError('publisher produced no frames')
            def media_probe():
                try:
                    output=inside(phone,sys.executable,Path(__file__).with_name('vpn_probe.py'),root,timeout=60).decode()
                except subprocess.CalledProcessError as error:
                    for line in (error.stdout or b'').decode(errors='replace').splitlines():
                        if re.fullmatch(r'VPN MEDIA FAILED: [a-z ]+; category [A-Za-z]+; HTTP [0-9]+',line):
                            print(line,flush=True)
                    raise
                if 'VPN MEDIA PASS:' not in output: raise RuntimeError('receiver failed')
            media_probe()
            print('VPN MEDIA PASS: real Rust + nginx + MediaMTX + kernel WireGuard; five decoded H264 frames and DELETE',flush=True)
            handshakes=inside(server,'wg','show','wg-nxrs','latest-handshakes').decode().split()
            if not handshakes or int(handshakes[-1])==0: raise RuntimeError('no authenticated VPN handshake')
            inside(server,'wg','set','wg-nxrs','peer',phone_public,'remove')
            if reachable(phone,'10.77.0.1',8443): raise RuntimeError('removed peer still reachable')
            print('VPN REVOCATION PASS: removed VPN peer cannot reconnect (not application-session revocation)',flush=True)
            replacement_secret=command(['wg','genkey'])
            replacement_public=command(['wg','pubkey'],replacement_secret).decode().strip()
            staged=parent/'rotation'
            vpn.vpn_peer.stage(root/'wg-nxrs.conf',root/'vpn-peers.json',staged,phone_public,replacement_public)
            unchanged={name:(root/name).read_bytes() for name in ['config.json','mediamtx.json','viewer.token','publisher.token','firewall.nft']}
            def install_pair(bundle,label):
                vpn.vpn_peer.check(root/'wg-nxrs.conf',root/'vpn-peers.json',bundle)
                # Only this hosted fixture installs files/recreates an owned interface.
                for name in ['wg-nxrs.conf','vpn-peers.json']:
                    pending=root/(name+'.pending')
                    vpn.vpn_peer.write_private(pending,(bundle/name).read_bytes());os.replace(pending,root/name)
                vpn.vpn_peer.sync_directory(root)
                _,_,selected,history=vpn.vpn_peer.pair(root/'wg-nxrs.conf',root/'vpn-peers.json')
                if any((root/name).read_bytes()!=value for name,value in unchanged.items()):
                    raise RuntimeError('unrelated enrollment changed')
                stripped=root/(label+'-server-wg.conf')
                private(stripped,command(['wg-quick','strip',root/'wg-nxrs.conf']))
                inside(server,'ip','link','delete','wg-nxrs')
                inside(server,'ip','link','add','wg-nxrs','type','wireguard')
                inside(server,'wg','setconf','wg-nxrs',stripped)
                inside(server,'ip','addr','add','10.77.0.1/32','dev','wg-nxrs')
                inside(server,'ip','link','set','wg-nxrs','mtu','1380','up')
                inside(server,'ip','route','add','10.77.0.2/32','dev','wg-nxrs')
                expected=[] if selected['peer_key'] is None else [selected['peer_key']]
                if inside(server,'wg','show','wg-nxrs','peers').decode().split()!=expected:
                    raise RuntimeError('restarted interface peer selection failed')
                return selected,history
            selected,history=install_pair(staged,'rotated')
            if history['generation']!=2 or history['retired_peers']!=[phone_public] or selected['peer_key']!=replacement_public:
                raise RuntimeError('persisted replacement history failed')
            # The phone still holds the OLD private key: repeated fresh TCP attempts must fail.
            for _ in range(3):
                if reachable(phone,'10.77.0.1',8443): raise RuntimeError('retired peer returned after restart')
            print('VPN PERSISTENCE PASS: saved replacement survives interface recreation; retired phone key cannot reconnect',flush=True)
            private(root/'rotated-phone-wg.conf',f'[Interface]\nPrivateKey = {replacement_secret.decode().strip()}\n[Peer]\nPublicKey = {server_public}\nEndpoint = 192.0.2.1:51820\nAllowedIPs = 10.77.0.1/32\nPersistentKeepalive = 25\n'.encode())
            inside(phone,'wg','setconf','wg-nxrs',root/'rotated-phone-wg.conf')
            for _ in range(30):
                if reachable(phone,'10.77.0.1',8443): break
                time.sleep(.1)
            else: raise RuntimeError('replacement peer failed to connect')
            handshakes=inside(server,'wg','show','wg-nxrs','latest-handshakes').decode().split()
            if len(handshakes)!=2 or handshakes[0]!=replacement_public or int(handshakes[1])==0:
                raise RuntimeError('replacement authenticated handshake missing')
            for port in [19999,18080,18081,8554,8889]:
                if reachable(phone,'10.77.0.1',port): raise RuntimeError('replacement private listener leaked')
            media_probe()
            inside(phone,'ip','route','replace','10.77.0.1/32','via','192.0.2.1','dev','uplink')
            if reachable(phone,'10.77.0.1',8443): raise RuntimeError('replacement underlay access leaked')
            inside(phone,'ip','route','replace','10.77.0.1/32','dev','wg-nxrs')
            inside(server,'nft','list','table','inet','nxrs_sentinel')
            try:
                vpn.vpn_peer.stage(root/'wg-nxrs.conf',root/'vpn-peers.json',parent/'forbidden-reenrollment',replacement_public,phone_public)
            except ValueError: pass
            else: raise RuntimeError('retired peer accepted for reenrollment')
            if (parent/'forbidden-reenrollment').exists(): raise RuntimeError('refused reenrollment wrote output')
            print('VPN ROTATION PASS: new authenticated peer decodes five fresh H264 frames; retired key stays refused; policy and application credentials retained',flush=True)
            progress=root/'active-progress.json';stop=root/'active-stop'
            holder=launch(phone,[sys.executable,Path(__file__).with_name('vpn_probe.py'),root,
                '--hold-progress',progress,'--stop',stop],'active-receiver')
            def observed_frames():
                if holder.poll() is not None:raise RuntimeError('active receiver stopped before controlled cleanup')
                try:value=read_progress(progress)
                except FileNotFoundError:return 0
                return value['frames']
            for _ in range(150):
                if observed_frames()>=10:break
                time.sleep(.1)
            else:raise RuntimeError('active VPN receiver did not advance before retirement')
            def source_frames():
                if source_process.poll() is not None:raise RuntimeError('source stopped during VPN retirement')
                values=re.findall(r'^frame=(\d+)',(root/'publisher.log').read_text(errors='replace'),re.M)
                return max(map(int,values),default=0)
            source_before=source_frames()
            retired=parent/'lost-peer'
            vpn.vpn_peer.stage_retirement(root/'wg-nxrs.conf',root/'vpn-peers.json',retired,replacement_public)
            selected,history=install_pair(retired,'retired')
            if selected['peer_key'] is not None or history['generation']!=3 or history['retired_peers']!=[phone_public,replacement_public]:
                raise RuntimeError('persisted lost-peer retirement history failed')
            for _ in range(3):
                if reachable(phone,'10.77.0.1',8443):raise RuntimeError('lost peer still reaches HTTPS')
            # Drain only bounded already-received images, then require sustained silence
            # while the independent loopback source is still producing new frames.
            deadline=time.monotonic()+8;stable_since=time.monotonic();previous=observed_frames()
            while time.monotonic()<deadline:
                current=observed_frames()
                if current<previous:raise RuntimeError('active receiver progress rolled back')
                if current!=previous:previous=current;stable_since=time.monotonic()
                if time.monotonic()-stable_since>=2:break
                time.sleep(.1)
            else:raise RuntimeError('lost peer active media kept advancing')
            if source_frames()<source_before+5:raise RuntimeError('retirement witness source did not independently advance')
            print('VPN LOST PEER PASS: no replacement key required; persisted empty peer set rejects reconnect and active media stops while source advances',flush=True)
            recovery_secret=command(['wg','genkey'])
            recovery_public=command(['wg','pubkey'],recovery_secret).decode().strip()
            recovered=parent/'recovered-peer'
            vpn.vpn_peer.stage_recovery(root/'wg-nxrs.conf',root/'vpn-peers.json',recovered,3,recovery_public)
            selected,history=install_pair(recovered,'recovered')
            if selected['peer_key']!=recovery_public or history['generation']!=4 or history['retired_peers']!=[phone_public,replacement_public]:
                raise RuntimeError('recovery did not preserve retired keys')
            for _ in range(3):
                if reachable(phone,'10.77.0.1',8443):raise RuntimeError('lost key revived during recovery')
            private(root/'recovered-phone-wg.conf',f'[Interface]\nPrivateKey = {recovery_secret.decode().strip()}\n[Peer]\nPublicKey = {server_public}\nEndpoint = 192.0.2.1:51820\nAllowedIPs = 10.77.0.1/32\nPersistentKeepalive = 25\n'.encode())
            inside(phone,'wg','setconf','wg-nxrs',root/'recovered-phone-wg.conf')
            for _ in range(30):
                if reachable(phone,'10.77.0.1',8443):break
                time.sleep(.1)
            else:raise RuntimeError('fresh recovery key cannot connect')
            handshakes=inside(server,'wg','show','wg-nxrs','latest-handshakes').decode().split()
            if len(handshakes)!=2 or handshakes[0]!=recovery_public or int(handshakes[1])==0:
                raise RuntimeError('fresh recovery handshake missing')
            private(stop,b'stop\n')
            if holder.wait(timeout=20)!=0:raise RuntimeError('retired active receiver cleanup failed')
            if 'VPN MEDIA PASS:' not in (root/'active-receiver.log').read_text(errors='replace'):
                raise RuntimeError('retired active receiver did not finish cleanup')
            media_probe()
            for port in [19999,18080,18081,8554,8889]:
                if reachable(phone,'10.77.0.1',port):raise RuntimeError('recovery private listener leaked')
            inside(phone,'ip','route','replace','10.77.0.1/32','via','192.0.2.1','dev','uplink')
            if reachable(phone,'10.77.0.1',8443):raise RuntimeError('recovery underlay access leaked')
            inside(phone,'ip','route','replace','10.77.0.1/32','dev','wg-nxrs')
            inside(server,'nft','list','table','inet','nxrs_sentinel')
            for forbidden in [phone_public,replacement_public]:
                try:vpn.vpn_peer.stage(root/'wg-nxrs.conf',root/'vpn-peers.json',parent/'forbidden-recovery',recovery_public,forbidden)
                except ValueError:pass
                else:raise RuntimeError('recovery reenrolled a retired key')
            if (parent/'forbidden-recovery').exists():raise RuntimeError('refused recovery wrote output')
            print('VPN LOST PEER RECOVERY PASS: both retired keys remain denied; fresh key decodes five frames; active receiver joins; application credentials and network policy retained',flush=True)


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
