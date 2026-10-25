#!/usr/bin/env python3
"""Hosted-only real systemd/nginx/gateway/MediaMTX test, synthetic loopback media.

Unique unit/account names and temporary binary/credential/state paths replace
installed paths; a no-op unit replaces network dependencies. All hardening stays
active. The separate VPN test covers the real tunnel. No home/device changes.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiortc import RTCSessionDescription
from revocation_reference import Reader, digest, private_write, require

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
sys.path.insert(0, str(DEPLOY))
import strict_vpn
import vpn
sys.path.insert(0,str(DEPLOY.parent/'tools'))
import gateway_proxy_evidence
import service_lifetime as lifetime


def command(args, *, check=True):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        raise RuntimeError('owned fixture command failed')
    return result


async def until(predicate, message, seconds=15):
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.15)
    raise AssertionError(message)


def prerequisites():
    require(os.geteuid() == 0 and sys.platform == 'linux', 'root Linux hosted runner required')
    require(os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'refuse non-hosted machine')
    require(Path('/proc/1/comm').read_text().strip() == 'systemd', 'real system service manager required')
    for name in ['systemctl', 'systemd-sysusers', 'runuser', 'userdel', 'openssl', 'wg', 'ffmpeg', 'install', 'nft']:
        require(shutil.which(name) is not None, 'required hosted tool missing')


def diagnostics(roles, conf, state):
    """Only allow-listed statuses/booleans/modes/categories; no raw logs/argv/URLs."""
    proof = state/'sandbox-proof.json'
    if proof.exists():
        source = json.loads(proof.read_text())
        result = {key: source.get(key) is True for key in ['nonroot', 'no_capabilities', 'no_new_privileges',
            'outside_write_denied', 'credential_source_denied', 'loaded_credentials_readable', 'staged_credential_private']}
        for key in ['loaded_mode', 'staged_mode']:
            value = source.get(key)
            if type(value) is int and 0 <= value <= 0o7777:
                result[key] = oct(value)
        print('SANDBOX DIAGNOSTIC:', json.dumps(result, sort_keys=True), flush=True)
    categories = ['permission denied', 'read-only file system', 'operation not permitted',
        'address already in use', 'private file', 'state directory', 'invalid configuration',
        'invalid security', 'revocation store', 'assertionerror', 'fastcgi', 'uwsgi', 'scgi',
        'no such file', 'credentials', 'unknown field', 'memory', 'failed at step',
        'address family', 'no such device', 'too many open files', 'authentication error',
        'no one is publishing', 'no supported codecs', 'failed to create', 'network is unreachable',
        'access denied','interactive authentication required','failed to connect to bus',
        'protected-parent','protected-file','file-digest','file-changed','policy-schema','policy-expiry',
        'source-policy','source-review-window','loader-output','loader-library-set','loader-status',
        'loader-budget','loader-deadline','runtime-directory','system-preload','proxy-exec',
        'credential activation window','finite credential activation window']
    for role, name in roles.items():
        record = {'role': role}
        for key in ['LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainCode', 'ExecMainStatus']:
            value = command(['systemctl', 'show', name+'.service', '-p', key, '--value'], check=False).stdout.decode().strip()
            record[key] = value if re.fullmatch(r'[A-Za-z0-9_-]{0,64}', value) else 'redacted'
        path = conf/(role+'.log')
        if path.exists():
            with path.open('rb') as stream:
                text = stream.read(65536).decode(errors='replace').lower()
            record['error_categories'] = [category for category in categories if category in text]
        print('SYSTEMD DIAGNOSTIC:', json.dumps(record, sort_keys=True), flush=True)


async def exercise(gateway, mediamtx, proxy_launcher, proxy_review, report):
    prerequisites()
    prefix = 'nxrs-sd-' + secrets.token_hex(4)
    control, media, proxy, target, network = [prefix + '-' + kind for kind in ['control', 'media', 'proxy', 'monitor', 'network']]
    stopper=target+'-stop'
    names = [stopper+'.service', control+'.service', media+'.service', proxy+'.service', target+'.target', network+'.service']
    root, state = Path('/run')/prefix, Path('/var/lib')/control
    require(not root.exists() and not state.exists(), 'new isolated fixture directories required')
    root.mkdir(mode=0o755)
    processes, logs, readers, written_units = [], [], [], []
    account_created = False
    management_filter = prefix.replace('-', '_')
    filter_created = False
    handler_refusals=0
    ports, used = {}, set()
    for key in ['https', 'media', 'ice', 'rtsp', 'api', 'auth', 'vpn', 'management']:
        while True:
            with socket.socket(type=socket.SOCK_DGRAM if key in ['ice', 'vpn'] else socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            if port not in used:
                used.add(port); ports[key] = port; break
    conf = root/'conf'
    try:
        strict_vpn.generate(conf, '127.0.0.1', '10.77.0.1', '10.77.0.2', 'endpoint.test',
                            base64.b64encode(secrets.token_bytes(32)).decode())
        require(json.loads((conf/'deployment.json').read_text())['mode'] == 'strict', 'complete strict bundle')
        (root/'bin').mkdir(mode=0o755)
        for source, name in [(gateway, 'gateway'), (mediamtx, 'mediamtx'), (proxy_launcher, 'proxy-launcher')]:
            shutil.copyfile(source, root/'bin'/name)
            (root/'bin'/name).chmod(0o755)
        artifact=root/'proxy-artifact';artifact.mkdir(mode=0o755)
        for source,name,mode in [('nginx','nginx',0o755),('review.json','approval.json',0o644)]:
            shutil.copyfile(proxy_review/source,artifact/name);(artifact/name).chmod(mode)
        approved=(artifact/'approval.json').read_bytes()
        policy=json.loads(approved)
        qualification_raw,qualification=gateway_proxy_evidence.read(proxy_review/'qualification.json')
        require(hashlib.sha256(qualification_raw).hexdigest()==policy['qualification_sha256'],'same reviewed qualification receipt')
        require(qualification['binary_sha256']==policy['binary_sha256'],'same reviewed qualified image')
        require(hashlib.sha256((artifact/'nginx').read_bytes()).hexdigest()==policy['binary_sha256'],'same reviewed proxy binary')
        for directory in ['deploy', 'tools']:
            (root/directory).mkdir(mode=0o755)
        for name in ['strict_vpn.py', 'vpn.py', 'vpn_peer.py']:
            shutil.copyfile(DEPLOY/name, root/'deploy'/name)
        shutil.copyfile(DEPLOY.parent/'tools/init_reference.py', root/'tools/init_reference.py')
        sysusers = conf/'account.conf'
        private_write(sysusers, f'u {control} - "nxrs disposable test" /nonexistent /usr/sbin/nologin\n')
        try:
            pwd.getpwnam(control)
        except KeyError:
            pass
        else:
            raise AssertionError('fixture account must not already exist')
        command(['systemd-sysusers', str(sysusers)])
        account_created = True
        user = pwd.getpwnam(control)
        state.mkdir(mode=0o700)
        os.chown(state, user.pw_uid, user.pw_gid)
        command(['runuser', '-u', control, '--', '/usr/bin/python3', str(root/'deploy/strict_vpn.py'), 'init-state', str(state)])
        snapshot = state/'revocations.json'
        require(json.loads(snapshot.read_text()) == {'version': 1, 'revoked': []}, 'explicit state initialization')
        second = secrets.token_hex(32)
        viewer = (conf/'viewer.token').read_text().strip()
        publisher = (conf/'publisher.token').read_text().strip()
        base = f'https://127.0.0.1:{ports["https"]}'
        internal = f'http://127.0.0.1:{ports["auth"]}'
        config = json.loads((conf/'config.json').read_text())
        config.update(listen=f'127.0.0.1:{ports["api"]}', auth_listen=f'127.0.0.1:{ports["auth"]}', media_origins=[base])
        config['cameras'][0]['whep_endpoint'] = base+'/reference/whep'
        config['viewers'].append(dict(viewer_id='viewer_other', site_id='site_reference',
            camera_ids=['cam_reference'], token_sha256=digest(second)))
        (conf/'config.json').write_text(json.dumps(config))
        security = json.loads((conf/'security.json').read_text())
        security.update(revocations_file=str(snapshot), media_api=f'127.0.0.1:{ports["management"]}')
        (conf/'security.json').write_text(json.dumps(security))
        (conf/'mediamtx.json').write_text(json.dumps(strict_vpn.media_config('127.0.0.1', ports, ports['management'])))
        (conf/'tls').mkdir(mode=0o700)
        command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(conf/'tls/privkey.pem'), '-out', str(conf/'tls/fullchain.pem'),
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'])
        (conf/'tls/privkey.pem').chmod(0o600)
        tls = ssl.create_default_context(cafile=str(conf/'tls/fullchain.pem'))
        (conf/'nginx.conf').write_text(vpn.nginx_config('127.0.0.1', '127.0.0.1', ports,
            cert=f'/run/credentials/{proxy}.service/server.crt', key=f'/run/credentials/{proxy}.service/server.key',
            runtime='/run/'+proxy))
        outside = root/'outside'
        outside.mkdir(mode=0o777); outside.chmod(0o777)
        probe = root/'bin/sandbox.py'
        shutil.copyfile(Path(__file__).with_name('sandbox_probe.py'), probe)
        probe.chmod(0o644)
        units = strict_vpn.services()
        mapping = {'nxrs-control': control, 'nxrs-media': media, 'nxrs-proxy': proxy, 'nxrs-monitor': target}
        for original in ['nxrs-control.service', 'nxrs-media.service', 'nxrs-proxy.service', 'nxrs-monitor-stop.service', 'nxrs-monitor.target']:
            text = units[original].replace('/etc/nxrs-monitor', str(conf))
            text = text.replace('/usr/local/libexec/nxrs-gateway-control', str(root/'bin/gateway'))
            text = text.replace('/usr/local/libexec/nxrs-mediamtx', str(root/'bin/mediamtx'))
            text = text.replace('/usr/local/libexec/nxrs-gateway-proxy', str(root/'bin/proxy-launcher'))
            text = text.replace('/usr/local/lib/nxrs-proxy', str(artifact))
            text = text.replace('wg-quick@wg-nxrs.service', network+'.service').replace('nxrs-firewall.service', network+'.service')
            filename = original
            for old, new in mapping.items():
                text = text.replace(old, new)
                filename = filename.replace(old, new)
            if original == 'nxrs-control.service':
                # Credentials are provisioned for the start/pre-start paths on systemd255;
                # do not use ExecStartPost as a proxy for the application's credential access.
                # Append after the production staging command and retain all sandbox checks.
                text += f'\n[Service]\nExecStartPre=/usr/bin/python3 {probe} {state} {outside} {conf}/security.json\n'
            if original.endswith('.service'):
                role = original.removeprefix('nxrs-').removesuffix('.service')
                private_write(conf/(role+'.log'), '')
                text += f'\n[Service]\nStandardOutput=append:{conf}/{role}.log\nStandardError=append:{conf}/{role}.log\n'
            destination = Path('/run/systemd/system')/filename
            require(not destination.exists(), 'new runtime unit required')
            destination.write_text(text); written_units.append(destination)
        destination = Path('/run/systemd/system')/(network+'.service')
        destination.write_text('[Service]\nType=oneshot\nExecStart=/usr/bin/true\nRemainAfterExit=yes\n')
        written_units.append(destination)
        command(['systemctl', 'daemon-reload'])

        def property_of(name, key):
            return command(['systemctl', 'show', name, '-p', key, '--value']).stdout.decode().strip()
        def service_state(name):
            return lifetime.parse(command(['systemctl','show',name+'.service','--all',*lifetime.PROPERTY_ARGS]).stdout)
        async def start():
            # Stop/join applications while the failure handler is still latched.
            command(['systemctl','stop',control+'.service',media+'.service',proxy+'.service',target+'.target'])
            await until(stopped,'join all application/pre-start/control/cgroup jobs before rearming failure cleanup')
            command(['systemctl','stop',stopper+'.service'])
            await until(stopped,'join failure latch release before a new activation')
            require(service_state(stopper)['ActiveState'] in ('inactive','failed'),'failure latch cleared')
            command(['systemctl', 'reset-failed', *names], check=False)
            command(['systemctl', 'start', target+'.target'], check=False)
        def launch_publisher():
            log = open(conf/f'encoder-{len(logs)}.log', 'wb'); logs.append(log)
            child = subprocess.Popen(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-re',
                '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10', '-an', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-tune', 'zerolatency', '-profile:v', 'baseline', '-bf', '0',
                '-g', '10', '-pix_fmt', 'yuv420p', '-f', 'rtsp', '-rtsp_transport', 'tcp',
                f'rtsp://publisher_reference:{publisher}@127.0.0.1:{ports["rtsp"]}/reference'],
                stdout=log, stderr=subprocess.STDOUT)
            processes.append(child)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            async def request(method, url, token=None, body=None):
                headers = {'Authorization': 'Bearer '+token} if token else {}
                async with http.request(method, url, headers=headers, json=body, ssl=tls, allow_redirects=False) as r:
                    raw = await r.read()
                    data = json.loads(raw) if raw and r.content_type == 'application/json' else None
                    return r.status, data
            async def ready():
                try:
                    code, body = await request('GET', internal+'/internal/viewers/viewer_reference/revocation')
                    if code != 200 or not body['media_control_ready']:
                        return False
                    # Type=simple/target active is not a bound/ready HTTPS listener.
                    # Verify the real frontend before running assertions or reconnecting.
                    code, health = await request('GET', base+'/healthz')
                    return code == 200 and health == {'status': 'ok'}
                except aiohttp.ClientConnectorCertificateError:
                    raise  # Never turn TLS verification failures into eventual success.
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return False
            def handler_epoch():
                return int(property_of(stopper+'.service','ExecMainStartTimestampMonotonic') or '0')
            async def handler_finished(previous):
                return lifetime.handler_finished(service_state(stopper),stopper+'.service',previous)
            async def refused_service(name):
                return property_of(name+'.service','Result')=='exit-code' and property_of(name+'.service','MainPID')=='0'
            async def stopped():
                return all(lifetime.quiescent(service_state(name),name+'.service',allow_latch=name==stopper)
                    for name in [control,media,proxy,stopper])
            async def publisher_ready():
                code, body = await request('GET', f'http://127.0.0.1:{ports["management"]}/v3/paths/get/reference', security['media_api_token'])
                return code == 200 and body.get('ready') is True
            async def connect(token):
                reader = Reader(); readers.append(reader)
                await reader.peer.setLocalDescription(await reader.peer.createOffer())
                code, grant = await request('POST', base+'/v1/cameras/cam_reference/playback-grants', token, {})
                require(code == 201, 'authorized grant through real nginx')
                async with http.post(grant['endpoint'], data=reader.peer.localDescription.sdp,
                    headers={'Authorization': 'Bearer '+grant['access_token'], 'Content-Type': 'application/sdp'},
                    ssl=tls, allow_redirects=False) as r:
                    if r.status != 201:
                        # Fixed categories only: upstream bodies can contain capability URLs,
                        # network identifiers or credentials and must never enter public logs.
                        sample = (await r.content.read(4096)).decode(errors='replace').lower()
                        categories = ['authentication error', 'address family', 'operation not permitted',
                            'permission denied', 'no such device', 'network is unreachable',
                            'no one is publishing', 'no supported codecs', 'unsupported',
                            'too many open files', 'deadline exceeded', 'invalid content-type',
                            'invalid sdp', 'bad gateway', 'not found', 'too many requests',
                            'failed to create', 'socket', 'ice', 'sdp']
                        print('WHEP DIAGNOSTIC:', json.dumps({'status': int(r.status),
                            'error_categories': [value for value in categories if value in sample]}), flush=True)
                        raise AssertionError('WHEP through generated proxy')
                    answer = await r.text()
                    reader.session_url = urljoin(grant['endpoint'], r.headers['Location'])
                    require(urlsplit(reader.session_url).netloc == urlsplit(base).netloc, 'same origin session')
                await reader.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
                async def progress(): return reader.count >= 5
                await until(progress, 'five advancing H264 frames')
                return reader
            async def frozen(reader):
                await asyncio.sleep(1.5)
                count = reader.count
                await asyncio.sleep(1)
                require(reader.count == count, 'continuously drained video stopped')

            await start()
            await until(ready, 'strict service group startup')
            require(property_of(target+'.target', 'ActiveState') == 'active', 'strict group active')
            proof = json.loads((state/'sandbox-proof.json').read_text())
            require(proof['uid'] == user.pw_uid and proof['outside_write_denied'] and proof['staged_credential_private'], 'sandbox probe executed')
            for service in [control, media, proxy]:
                pid = int(property_of(service+'.service', 'MainPID'))
                status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                require(int(status['Uid'].split()[0]) != 0 and int(status['CapEff'].strip(), 16) == 0
                        and status['NoNewPrivs'].strip() == '1', 'real process privilege restrictions')
            proxy_pid=int(property_of(proxy+'.service','MainPID'))
            images=set()
            for line in Path(f'/proc/{proxy_pid}/maps').read_text().splitlines():
                parts=line.split(maxsplit=5)
                if len(parts)==6 and 'x' in parts[1] and parts[5].startswith('/'):
                    images.add(Path(parts[5]).resolve())
            library_paths=dict(loader='/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2',
                libc='/usr/lib/x86_64-linux-gnu/libc.so.6',crypto='/usr/lib/x86_64-linux-gnu/libcrypto.so.3',
                ssl='/usr/lib/x86_64-linux-gnu/libssl.so.3')
            require(images=={artifact/'nginx',*(Path(p) for p in library_paths.values())},'exact actual proxy executable mappings')
            executed_images={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in library_paths.items()}
            require(executed_images==policy['libraries'],'same approved actually mapped libraries')
            executed_images['nginx']=hashlib.sha256((artifact/'nginx').read_bytes()).hexdigest()
            require(executed_images['nginx']==policy['binary_sha256'],'same approved actually mapped nginx')
            require((await request('GET', base+'/v1/cameras'))[0] == 401, 'network access is not application authorization')
            require((await request('GET', base+'/internal/viewers/viewer_reference/revocation'))[0] == 404, 'private admin not proxied')
            require((await request('GET', base+'/v3/webrtcsessions/list', viewer))[0] == 404, 'management not proxied')
            print('PASS: generated strict services run under real systemd with credential and filesystem restrictions', flush=True)
            launch_publisher(); await until(publisher_ready, 'reference publisher ready')
            a, b = await connect(viewer), await connect(second)
            code, _ = await request('POST', internal+'/internal/viewers/viewer_reference/revoke', body={})
            require(code == 202, 'durable revocation accepted')
            await frozen(a)
            before = b.count; await asyncio.sleep(0.8)
            require(b.count > before, 'unrelated viewing continues after selective revocation')
            require('viewer_reference' in json.loads(snapshot.read_text())['revoked'], 'denial persisted by service user')
            print('PASS: selective revocation stops active video and persists under the installed service permissions', flush=True)
            previous=handler_epoch()
            os.kill(int(property_of(control+'.service', 'MainPID')), signal.SIGKILL)
            await until(lambda: handler_finished(previous),'controller crash triggers and joins the failure handler')
            await until(stopped, 'controller crash must stop media and HTTPS services')
            await frozen(b)
            require(property_of(target+'.target', 'ActiveState') != 'active', 'failed group not reported active')
            print('PASS: abrupt controller death stops media and proxy; existing video ceases without an app request', flush=True)
            await start(); await until(ready, 'explicit strict group recovery')
            require((await request('GET', base+'/v1/cameras', viewer))[0] == 403, 'restart must retain revoked identity')
            require((await request('GET', base+'/v1/cameras', second))[0] == 200, 'healthy account survives group restart')
            launch_publisher(); await until(publisher_ready, 'publisher reconnect after fail-stop')
            await connect(second)
            print('PASS: explicit group recovery retains denial and restores authorized video', flush=True)
            command(['systemctl', 'stop', target+'.target'])
            await until(stopped, 'group stop')
            preserved = snapshot.read_bytes()
            for failure in ['corrupt', 'missing']:
                if failure == 'corrupt': snapshot.write_bytes(b'{')
                else: snapshot.unlink()
                previous=handler_epoch()
                await start()
                await until(lambda: refused_service(control), 'invalid state must produce actual controller refusal')
                await until(lambda: handler_finished(previous),'invalid state triggers and joins the failure handler')
                handler_refusals+=1
                await until(stopped, 'invalid state startup must fail closed')
                require(not snapshot.exists() if failure == 'missing' else snapshot.read_bytes() == b'{', 'service startup must never reseed revocations')
                command(['systemctl', 'stop', target+'.target'], check=False)
                snapshot.write_bytes(preserved); snapshot.chmod(0o600)
                os.chown(snapshot, user.pw_uid, user.pw_gid)
            print('PASS: missing and corrupt state refuse startup without recreating an empty deny list', flush=True)
            # Root changes only the disposable installed fixture, never system libraries.
            original=(artifact/'nginx').read_bytes()
            cases=['missing-image','changed-image','symlink-image','writable-image','changed-library-pin','expired-policy','missing-policy','duplicate-policy']
            for case in cases:
                command(['systemctl','stop',target+'.target'],check=False)
                await until(stopped,'stop before artifact refusal')
                image=artifact/'nginx';approval=artifact/'approval.json'
                if case in ['missing-image','symlink-image']:
                    image.rename(artifact/'saved-image')
                    if case=='symlink-image':image.symlink_to(artifact/'saved-image')
                elif case=='changed-image':image.write_bytes(b'X'+original[1:])
                elif case=='writable-image':image.chmod(0o777)
                elif case=='missing-policy':approval.rename(artifact/'saved-policy')
                elif case=='duplicate-policy':approval.write_bytes(b'{"schema":1,'+approved[1:])
                else:
                    changed=json.loads(approved)
                    if case=='changed-library-pin':changed['libraries']['ssl']='0'*64
                    else:changed['valid_until']=changed['reviewed_at']
                    approval.write_text(json.dumps(changed))
                try:
                    previous=handler_epoch()
                    await start()
                    await until(lambda: refused_service(proxy),'proxy artifact admission must refuse '+case)
                    await until(lambda: handler_finished(previous),'proxy refusal triggers and joins the failure handler')
                    handler_refusals+=1
                    await until(stopped,'invalid proxy must stop the strict group: '+case)
                    require(property_of(target+'.target','ActiveState')!='active','invalid artifact cannot report active')
                    print('PROXY_ADMISSION_CASE '+json.dumps(dict(case=case,refused=True,all_main_processes_stopped=True)),flush=True)
                except Exception:
                    # Capture fixed state before explicit finally cleanup can hide live siblings.
                    print('PROXY_ADMISSION_FAILURE '+json.dumps(dict(case=case,
                        live_roles=[role for role,name in [('control',control),('media',media),('proxy',proxy)]
                            if property_of(name+'.service','MainPID')!='0'],
                        target_active=property_of(target+'.target','ActiveState')=='active')),flush=True)
                    raise
                finally:
                    command(['systemctl','stop',target+'.target'],check=False)
                    await until(stopped,'join refused group before restoring artifacts')
                    if image.is_symlink():image.unlink()
                    if (artifact/'saved-image').exists():(artifact/'saved-image').rename(image)
                    else:image.write_bytes(original)
                    image.chmod(0o755)
                    if (artifact/'saved-policy').exists():(artifact/'saved-policy').rename(approval)
                    approval.write_bytes(approved);approval.chmod(0o644)
            await start();await until(ready,'explicit recovery after artifact refusals')
            require((await request('GET',base+'/v1/cameras',second))[0]==200,'approved bundle restores authorized HTTPS')
            launch_publisher();await until(publisher_ready,'publisher before management-loss witness')
            live=await connect(second)
            previous=handler_epoch()
            command(['nft','add','table','inet',management_filter]);filter_created=True
            command(['nft','add','chain','inet',management_filter,'management',
                '{','type','filter','hook','output','priority','0;','policy','accept;','}'])
            command(['nft','add','rule','inet',management_filter,'management','ip','daddr','127.0.0.1',
                'tcp','dport',str(ports['management']),'counter','reject','with','tcp','reset'])
            before=live.count;await asyncio.sleep(0.5)
            require(live.count>before and property_of(media+'.service','MainPID')!='0',
                'management fault leaves the existing media path initially live')
            await until(lambda: handler_finished(previous),'management loss retires control and joins cleanup')
            await until(stopped,'management loss must stop all application and cgroup jobs')
            await frozen(live)
            require(property_of(control+'.service','Result')=='exit-code',
                'management loss must produce an actual controller failure')
            rules=json.loads(command(['nft','-j','list','chain','inet',management_filter,'management']).stdout)
            require(any(expr.get('counter',{}).get('packets',0)>0 for item in rules['nftables']
                for expr in item.get('rule',{}).get('expr',[])),'actual owned management filter executed')
            command(['nft','delete','table','inet',management_filter]);filter_created=False
            await start();await until(ready,'explicit recovery after management loss')
            require((await request('GET',base+'/v1/cameras',viewer))[0]==403,'management recovery retains denial')
            launch_publisher();await until(publisher_ready,'publisher after management recovery')
            await connect(second)
            print('PASS: loss of only management retires the strict group and active video; explicit recovery retains denial',flush=True)
            config_path=conf/'config.json';valid_config=config_path.read_bytes()
            for case in ['missing','expired']:
                command(['systemctl','stop',control+'.service',media+'.service',proxy+'.service',target+'.target'])
                await until(stopped,'join group before credential-window refusal')
                changed=json.loads(valid_config)
                if case=='missing':changed.pop('credential_window')
                else:changed['credential_window']={'not_before':int(time.time())-10,'expires_at':int(time.time())}
                config_path.write_text(json.dumps(changed));previous=handler_epoch()
                try:
                    await start()
                    await until(lambda: refused_service(control),'credential window must produce actual controller refusal')
                    await until(lambda: handler_finished(previous),'credential refusal joins a fresh handler')
                    await until(stopped,'credential refusal joins all application/cgroup jobs')
                    handler_refusals+=1
                finally:
                    command(['systemctl','stop',control+'.service',media+'.service',proxy+'.service',target+'.target'])
                    await until(stopped,'join refused activation before replacing credential policy')
                    config_path.write_bytes(valid_config)
            short=json.loads(valid_config)
            short['credential_window']={'not_before':int(time.time())-1,'expires_at':int(time.time())+15}
            config_path.write_text(json.dumps(short));await start();await until(ready,'finite credential activation')
            launch_publisher();await until(publisher_ready,'publisher before credential expiry')
            expiring=await connect(second);previous=handler_epoch()
            expiry_log_start=(conf/'control.log').stat().st_size
            await until(lambda: handler_finished(previous),'credential expiry fails the actual controller',seconds=20)
            await until(stopped,'credential expiry joins media/proxy and active video')
            await frozen(expiring)
            require(property_of(control+'.service','Result')=='exit-code','credential expiry is controller failure')
            require(time.time()>=short['credential_window']['expires_at'],'controller retirement follows configured expiry')
            with (conf/'control.log').open('rb') as log:
                log.seek(expiry_log_start);reason=log.read(65536)
            require(b'nxrs-gateway: credential activation window unavailable' in reason,
                'typed credential expiry reason must be witnessed in the actual controller')
            old_viewer,old_second,old_publisher,old_management=viewer,second,publisher,security['media_api_token']
            viewer,second,publisher=secrets.token_hex(32),secrets.token_hex(32),secrets.token_hex(32)
            security['media_api_token']=secrets.token_hex(32)
            rotated=json.loads(valid_config)
            rotated['credential_window']={'not_before':int(time.time())-1,'expires_at':int(time.time())+3600}
            for principal in rotated['viewers']:
                principal['token_sha256']=digest(viewer if principal['viewer_id']=='viewer_reference' else second)
            rotated['publishers'][0]['token_sha256']=digest(publisher)
            config_path.write_text(json.dumps(rotated));(conf/'security.json').write_text(json.dumps(security))
            await start();await until(ready,'explicit rotated credential cohort recovery')
            for old in [old_viewer,old_second]:
                require((await request('GET',base+'/v1/cameras',old))[0]==401,'old viewer credential rejected after rotation')
            require((await request('GET',base+'/v1/cameras',viewer))[0]==403,'new token for denied viewer remains denied')
            old_auth={'action':'publish','protocol':'rtsp','path':'reference','user':'publisher_reference',
                'password':old_publisher,'token':old_publisher}
            require((await request('POST',internal+'/internal/mediamtx/auth',body=old_auth))[0]==401,
                'old publisher credential rejected after rotation')
            require((await request('GET',f'http://127.0.0.1:{ports["management"]}/v3/webrtcsessions/list?itemsPerPage=256',old_management))[0]==401,
                'old management credential rejected after rotation')
            launch_publisher();await until(publisher_ready,'rotated publisher admitted')
            await connect(second)
            print('PASS: finite credential expiry stops active media; controlled rotation rejects old keys and preserves denial',flush=True)
            command(['systemctl','stop',target+'.target']);await until(stopped,'final service join')
            require(hashlib.sha256((artifact/'nginx').read_bytes()).hexdigest()==policy['binary_sha256'],'unchanged final approved image')
            source=Path(__file__).resolve().parents[2]
            sys.path.insert(0,str(source/'tools/native-packages'))
            import candidate
            head=command(['/usr/bin/git','-c','safe.directory='+str(source),'-C',str(source),'rev-parse','HEAD']).stdout.decode().strip()
            require(handler_refusals==12,'all actual failure handler invocations observed')
            receipt=dict(schema=1,scope='hosted-generated-systemd-approved-camera-proxy',qualified=True,
                checkout=dict(commit=head,clean=not command(['/usr/bin/git','-c','safe.directory='+str(source),'-C',str(source),'status','--porcelain']).stdout.strip()),
                inputs=candidate.source_inputs(),review_sha256=hashlib.sha256(approved).hexdigest(),
                binary_sha256=policy['binary_sha256'],launcher_sha256=hashlib.sha256(proxy_launcher.read_bytes()).hexdigest(),
                libraries=policy['libraries'],executed_images=executed_images,qualification_sha256=policy['qualification_sha256'],artifact_refusals=cases,real_https=True,nonroot_sandbox=True,
                selective_revocation=True,controller_crash_failstop=True,durable_denial_recovery=True,
                missing_corrupt_state_refused=True,artifact_recovery=True,failure_handler_refusals=handler_refusals,
                management_loss_failstop=True,management_loss_filter_executed=True,management_loss_recovery=True,
                credential_window_refusals=2,credential_expiry_failstop=True,credential_expiry_reason_witnessed=True,credential_rotation_recovery=True,
                old_viewer_publisher_management_rejected=True,rotated_identity_denial_preserved=True,
                physical_qualified=False,full_native_advisory_closure=False,fips_validated=False,deployment_permitted=False)
            require(receipt['checkout']['clean'],'unchanged clean qualification checkout')
            require(qualification['checkout']==receipt['checkout'] and qualification['inputs']==receipt['inputs'],
                'same complete proxy build and service source vector')
            require(qualification['libraries']=={path:policy['libraries'][name] for name,path in library_paths.items()},
                'same build approved and actually executed library cohort')
            print('PASS: eight proxy artifact admission refusals stop the strict group; approved bundle restores HTTPS',flush=True)

    except Exception:
        try: diagnostics({'control': control, 'media': media, 'proxy': proxy,'monitor-stop':stopper}, conf, state)
        except Exception: print('SYSTEMD DIAGNOSTIC: unavailable', flush=True)
        raise
    finally:
        failures = []
        if filter_created:
            try:command(['nft','delete','table','inet',management_filter])
            except Exception:failures.append('management filter')
        for reader in readers:
            try: await reader.close()
            except Exception: failures.append('peer')
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
                    try: await asyncio.to_thread(process.wait, timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); await asyncio.to_thread(process.wait, timeout=5)
            except Exception: failures.append('publisher')
        for handle in logs:
            try: handle.close()
            except Exception: failures.append('log')
        for args in [['systemctl', 'stop', *names], ['systemctl', 'reset-failed', *names]]:
            try: command(args, check=False)
            except Exception: failures.append('unit stop')
        for path in written_units:
            try: path.unlink(missing_ok=True)
            except Exception: failures.append('unit file')
        try: command(['systemctl', 'daemon-reload'])
        except Exception: failures.append('reload')
        for path in [state, root]:
            try:
                if path.exists(): shutil.rmtree(path)
            except Exception: failures.append('private fixture')
        if account_created:
            try: command(['userdel', control])
            except Exception: failures.append('account')
        require(not failures, 'owned systemd fixture cleanup incomplete')
    for path,raw in [(report.with_name(report.stem+'-review.json'),approved),
                     (report.with_name(report.stem+'-qualification.json'),qualification_raw)]:
        with open(path,'xb') as out:out.write(raw)
    with open(report,'x') as out:json.dump(receipt,out,sort_keys=True,indent=2)
    print('SYSTEMD REFERENCE PASS: strict services, real synthetic media, fail-stop and durable denial; no phone/camera/VPN claim', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', type=Path, required=True)
    parser.add_argument('--mediamtx', type=Path, required=True)
    parser.add_argument('--proxy-launcher',type=Path,required=True)
    parser.add_argument('--proxy-review',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    try:
        asyncio.run(exercise(args.gateway.resolve(), args.mediamtx.resolve(),args.proxy_launcher.resolve(),args.proxy_review.resolve(),args.report))
    except Exception as error:
        print('SYSTEMD REFERENCE FAILED:', str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
