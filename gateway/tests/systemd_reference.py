#!/usr/bin/env python3
"""Hosted-only real systemd/nginx/gateway/MediaMTX test, synthetic loopback media.

Unique unit/account names and temporary binary/credential/state paths replace
installed paths; a no-op unit replaces network dependencies. All hardening stays
active. The separate VPN test covers the real tunnel. No home/device changes.
"""
import argparse
import asyncio
import base64
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
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiortc import RTCSessionDescription
from revocation_reference import Reader, digest, private_write, require

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
sys.path.insert(0, str(DEPLOY))
import strict_vpn
import vpn


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
    for name in ['systemctl', 'systemd-sysusers', 'runuser', 'userdel', 'openssl', 'nginx', 'wg', 'ffmpeg', 'install']:
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
        'no one is publishing', 'no supported codecs', 'failed to create', 'network is unreachable']
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


async def exercise(gateway, mediamtx):
    prerequisites()
    prefix = 'nxrs-sd-' + secrets.token_hex(4)
    control, media, proxy, target, network = [prefix + '-' + kind for kind in ['control', 'media', 'proxy', 'monitor', 'network']]
    names = [control+'.service', media+'.service', proxy+'.service', target+'.target', network+'.service']
    root, state = Path('/run')/prefix, Path('/var/lib')/control
    require(not root.exists() and not state.exists(), 'new isolated fixture directories required')
    root.mkdir(mode=0o755)
    processes, logs, readers, written_units = [], [], [], []
    account_created = False
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
        for source, name in [(gateway, 'gateway'), (mediamtx, 'mediamtx')]:
            shutil.copyfile(source, root/'bin'/name)
            (root/'bin'/name).chmod(0o755)
        for directory in ['deploy', 'tools']:
            (root/directory).mkdir(mode=0o755)
        for name in ['strict_vpn.py', 'vpn.py']:
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
        for original in ['nxrs-control.service', 'nxrs-media.service', 'nxrs-proxy.service', 'nxrs-monitor.target']:
            text = units[original].replace('/etc/nxrs-monitor', str(conf))
            text = text.replace('/usr/local/libexec/nxrs-gateway-control', str(root/'bin/gateway'))
            text = text.replace('/usr/local/libexec/nxrs-mediamtx', str(root/'bin/mediamtx'))
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
        def start():
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
                    return code == 200 and body['media_control_ready']
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return False
            async def stopped():
                return all(property_of(name+'.service', 'MainPID') == '0' for name in [control, media, proxy])
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

            start()
            await until(ready, 'strict service group startup')
            require(property_of(target+'.target', 'ActiveState') == 'active', 'strict group active')
            proof = json.loads((state/'sandbox-proof.json').read_text())
            require(proof['uid'] == user.pw_uid and proof['outside_write_denied'] and proof['staged_credential_private'], 'sandbox probe executed')
            for service in [control, media, proxy]:
                pid = int(property_of(service+'.service', 'MainPID'))
                status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                require(int(status['Uid'].split()[0]) != 0 and int(status['CapEff'].strip(), 16) == 0
                        and status['NoNewPrivs'].strip() == '1', 'real process privilege restrictions')
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
            os.kill(int(property_of(control+'.service', 'MainPID')), signal.SIGKILL)
            await until(stopped, 'controller crash must stop media and HTTPS services')
            await frozen(b)
            require(property_of(target+'.target', 'ActiveState') != 'active', 'failed group not reported active')
            print('PASS: abrupt controller death stops media and proxy; existing video ceases without an app request', flush=True)
            start(); await until(ready, 'explicit strict group recovery')
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
                start(); await until(stopped, 'invalid state startup must fail closed')
                require(not snapshot.exists() if failure == 'missing' else snapshot.read_bytes() == b'{', 'service startup must never reseed revocations')
                command(['systemctl', 'stop', target+'.target'], check=False)
                snapshot.write_bytes(preserved); snapshot.chmod(0o600)
                os.chown(snapshot, user.pw_uid, user.pw_gid)
            print('PASS: missing and corrupt state refuse startup without recreating an empty deny list', flush=True)
    except Exception:
        try: diagnostics({'control': control, 'media': media, 'proxy': proxy}, conf, state)
        except Exception: print('SYSTEMD DIAGNOSTIC: unavailable', flush=True)
        raise
    finally:
        failures = []
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
    print('SYSTEMD REFERENCE PASS: strict services, real synthetic media, fail-stop and durable denial; no phone/camera/VPN claim', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', type=Path, required=True)
    parser.add_argument('--mediamtx', type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(exercise(args.gateway.resolve(), args.mediamtx.resolve()))
    except Exception as error:
        print('SYSTEMD REFERENCE FAILED:', str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
