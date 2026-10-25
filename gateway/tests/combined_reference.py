#!/usr/bin/env python3
"""Hosted-only full strict service group + actual Rust source + real WireGuard.

All network changes occur in two new namespaces. Runtime units/accounts/files
are uniquely owned and removed. No sensor, home network or iPhone is accessed.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import sys

from combined_viewer import HEALTH_TIMEOUT
import service_lifetime as lifetime

ROLES = ('control', 'media', 'proxy', 'ingest')
HERE = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def internal_request(http, method, path, body=None):
    """Status-only revocation acknowledgments have no JSON response body."""
    async with http.request(method, 'http://127.0.0.1:18081' + path, json=body,
                            allow_redirects=False) as response:
        payload = await response.read()
        return response.status, json.loads(payload) if payload else None


def command(args, data=None, check=True):
    result = subprocess.run([str(a) for a in args], input=data, capture_output=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError('owned fixture command failed')
    return result.stdout


def prerequisites():
    require(sys.platform == 'linux' and os.geteuid() == 0
            and os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'refuse non-hosted machine')
    require(hasattr(os, 'setns') and Path('/proc/1/comm').read_text().strip() == 'systemd',
            'Python 3.12 and real system manager required')
    for tool in ['ip', 'nft', 'wg', 'wg-quick', 'systemctl', 'systemd-sysusers', 'userdel',
                 'openssl', 'ffmpeg', 'nginx']:
        require(shutil.which(tool) is not None, 'required hosted tool missing')


def adapt_unit(text, original, prefix, root, server):
    """Only paths/names and namespace placement change; do not remove restrictions."""
    text = text.replace('/etc/nxrs-monitor', str(root / 'conf'))
    text = text.replace('/usr/local/lib/nxrs-proxy',str(root/'proxy-artifact'))
    for binary, installed in [('gateway', 'nxrs-gateway-control'), ('media', 'nxrs-mediamtx'),
                              ('ingest', 'nxrs-camera-ingest'), ('media-sandbox', 'nxrs-media-sandbox'),
                              ('proxy-launcher','nxrs-gateway-proxy')]:
        text = text.replace('/usr/local/libexec/' + installed, str(root / 'bin' / binary))
    mapping = {'wg-quick@wg-nxrs': prefix + '-network', 'nxrs-firewall': prefix + '-firewall',
               'nxrs-monitor': prefix + '-monitor', **{'nxrs-' + r: prefix + '-' + r for r in ROLES}}
    name = original
    for old, new in mapping.items():
        text = text.replace(old, new)
        name = name.replace(old, new)
    if original.endswith('.service'):
        text += f'\n[Service]\nNetworkNamespacePath=/run/netns/{server}\n'
    return name, text


def cleanup_all(actions):
    """Attempt every cleanup even after one fails; report only fixed failure categories."""
    failures = []
    for label, action in actions:
        try:
            action()
        except Exception:
            failures.append(label)
    return failures


class Networks:
    def __init__(self, prefix):
        self.server, self.phone = prefix + '-s', prefix + '-p'
        self.owned = []
        self.original = None

    def setup(self):
        self.original = os.open('/proc/self/ns/net', os.O_RDONLY)
        for ns in [self.server, self.phone]:
            command(['ip', 'netns', 'add', ns])
            self.owned.append(ns)
            command(['ip', '-n', ns, 'link', 'set', 'lo', 'up'])
        self.inside(self.server, 'ip', 'link', 'add', 'uplink', 'type', 'veth', 'peer', 'name', 'peerlink')
        self.inside(self.server, 'ip', 'link', 'set', 'peerlink', 'netns', self.phone)
        self.inside(self.phone, 'ip', 'link', 'set', 'peerlink', 'name', 'uplink')
        for ns, address in [(self.server, '192.0.2.1/30'), (self.phone, '192.0.2.2/30')]:
            self.inside(ns, 'ip', 'addr', 'add', address, 'dev', 'uplink')
            self.inside(ns, 'ip', 'link', 'set', 'uplink', 'up')
        fd = os.open('/run/netns/' + self.server, os.O_RDONLY)
        try:
            os.setns(fd, os.CLONE_NEWNET)
        finally:
            os.close(fd)

    @staticmethod
    def inside(ns, *args, data=None):
        return command(['ip', 'netns', 'exec', ns, *args], data=data)

    def close(self):
        actions = []
        if self.original is not None:
            actions += [('restore namespace', lambda: os.setns(self.original, os.CLONE_NEWNET)),
                        ('namespace descriptor', lambda: os.close(self.original))]
        actions += [('namespace', lambda ns=ns: command(['ip', 'netns', 'delete', ns]))
                    for ns in reversed(self.owned)]
        require(not cleanup_all(actions), 'namespace cleanup incomplete')


async def until(predicate, message, seconds=25):
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        if await predicate():
            return
        await asyncio.sleep(.1)
    raise AssertionError(message)


class Phone:
    def __init__(self, child):
        self.child = child

    @classmethod
    async def start(cls, namespace, config):
        child = await asyncio.create_subprocess_exec('ip', 'netns', 'exec', namespace,
            sys.executable, str(HERE / 'combined_viewer.py'), str(config), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024)
        return cls(child)

    async def ask(self, op, **fields):
        self.child.stdin.write((json.dumps(dict(op=op, **fields)) + '\n').encode())
        await asyncio.wait_for(self.child.stdin.drain(), 5)
        budget = HEALTH_TIMEOUT + 10 if op == 'health' else 25
        raw = await asyncio.wait_for(self.child.stdout.readline(), budget)
        require(raw and len(raw) <= 1024, 'bounded phone response')
        result = json.loads(raw)
        require('error' not in result, 'remote viewer operation failed')
        return result

    async def close(self):
        if self.child.returncode is None:
            try:
                await self.ask('quit')
                self.child.stdin.close()
                await asyncio.wait_for(self.child.wait(), 20)
                require(self.child.returncode == 0, 'remote viewer cleanup succeeded')
            except BaseException:
                if self.child.returncode is None:
                    self.child.kill()
                    await asyncio.wait_for(self.child.wait(), 5)
                raise


async def exercise(args, prefix, nets):
    import aiohttp
    from revocation_reference import digest, private_write
    from rust_camera import RustCamera
    sys.path.insert(0, str(HERE.parent / 'deploy'))
    import strict_vpn
    import vpn

    root, state = Path('/run') / prefix, Path('/var/lib') / (prefix + '-control')
    require(not root.exists() and not state.exists(), 'new private fixture required')
    root.mkdir(mode=0o755)
    conf = root / 'conf'
    unit_files, names = [], []
    account_created = False
    camera = phone = None
    stage = 'setup'
    roles = {r: prefix + '-' + r for r in ROLES}
    target, network = prefix + '-monitor.target', prefix + '-network.service'
    runtime_units = Path('/run/systemd/system')

    def prop(role, key):
        return command(['systemctl', 'show', roles[role] + '.service', '-p', key, '--value']).decode().strip()

    stopper=prefix+'-monitor-stop.service'
    def handler_epoch():
        return int(command(['systemctl','show',stopper,'-p','ExecMainStartTimestampMonotonic','--value']).decode().strip() or '0')
    def service_state(unit):
        return lifetime.parse(command(['systemctl','show',unit,'--all',*lifetime.PROPERTY_ARGS]))
    async def handler_finished(previous):
        return lifetime.handler_finished(service_state(stopper),stopper,previous)

    async def start():
        command(['systemctl','stop',*(roles[r]+'.service' for r in ROLES),target])
        await until(stopped,'join all application/pre-start/control/cgroup jobs before rearming failure cleanup')
        command(['systemctl','stop',stopper])
        await until(stopped,'join failure latch release before activation')
        require(service_state(stopper)['ActiveState'] in ('inactive','failed'),'failure latch cleared')
        command(['systemctl', 'reset-failed', *names], check=False)
        command(['systemctl', 'start', target])

    async def stopped():
        return (all(lifetime.quiescent(service_state(roles[r]+'.service'),roles[r]+'.service') for r in ROLES)
            and lifetime.quiescent(service_state(stopper),stopper,allow_latch=True))

    def cgroup_pids(path):
        if not path.exists():
            return set()
        return {int(pid) for file in path.rglob('cgroup.procs') for pid in file.read_text().split()}

    try:
        (root / 'bin').mkdir(mode=0o755)
        for path, name in [(args.gateway, 'gateway'), (args.mediamtx, 'media'), (args.ingest, 'ingest'),
                           (args.publisher, 'publisher'), (args.ingest.parent/'nxrs-media-sandbox','media-sandbox'),
                           (args.proxy_launcher,'proxy-launcher')]:
            shutil.copyfile(path, root / 'bin' / name)
            (root / 'bin' / name).chmod(0o755)
        artifact=root/'proxy-artifact';artifact.mkdir(mode=0o755)
        for source,name,mode in [('nginx','nginx',0o755),('review.json','approval.json',0o644)]:
            shutil.copyfile(args.proxy_review/source,artifact/name);(artifact/name).chmod(mode)
        images = root / 'images'
        images.mkdir(mode=0o700)
        camera = await RustCamera.start(args.camera_source, images)
        private = command(['wg', 'genkey'])
        public = command(['wg', 'pubkey'], private).decode().strip()
        strict_vpn.generate(conf, '10.77.0.1', '10.77.0.1', '10.77.0.2', '192.0.2.1', public,
            camera=f'127.0.0.1:{camera.port}', width=320, height=240,
            allow_insecure_camera_http=True, source_kind='synthetic')
        require(json.loads((conf / 'deployment.json').read_text())['includes_ingest'], 'complete ingest bundle')
        config = json.loads((conf / 'config.json').read_text())
        second = secrets.token_hex(32)
        config['viewers'].append(dict(viewer_id='viewer_other', site_id='site_reference',
            camera_ids=['cam_reference'], token_sha256=digest(second)))
        (conf / 'config.json').write_text(json.dumps(config))
        security = json.loads((conf / 'security.json').read_text())
        security['revocations_file'] = str(state / 'revocations.json')
        (conf / 'security.json').write_text(json.dumps(security))
        ingest_config = json.loads((conf / 'ingest.json').read_text())
        ingest_config['publisher_token_file'] = '/run/' + roles['ingest'] + '/publisher.token'
        ingest_config['publisher'] = str(root / 'bin' / 'publisher')
        ingest_config['media_sandbox'] = str(root/'bin'/'media-sandbox')
        (conf / 'ingest.json').write_text(json.dumps(ingest_config))
        try:
            pwd.getpwnam(roles['control'])
        except KeyError:
            pass
        else:
            raise AssertionError('new service account required')
        private_write(conf / 'account.conf', f'u {roles["control"]} - "nxrs isolated test" /nonexistent /usr/sbin/nologin\n')
        command(['systemd-sysusers', conf / 'account.conf'])
        account_created = True
        owner = pwd.getpwnam(roles['control'])
        state.mkdir(mode=0o700)
        # Explicit first enrollment before service start; no boot/recovery reseeding.
        strict_vpn.initialize_state(state)
        os.chown(state / 'revocations.json', owner.pw_uid, owner.pw_gid)
        os.chown(state, owner.pw_uid, owner.pw_gid)
        (conf / 'tls').mkdir(mode=0o700)
        command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=isolated', '-addext', 'subjectAltName=IP:10.77.0.1',
            '-keyout', conf / 'tls/privkey.pem', '-out', conf / 'tls/fullchain.pem'])
        (conf / 'tls/privkey.pem').chmod(0o600)
        (conf / 'nginx.conf').write_text(vpn.nginx_config('10.77.0.1', '10.77.0.1',
            cert=f'/run/credentials/{roles["proxy"]}.service/server.crt',
            key=f'/run/credentials/{roles["proxy"]}.service/server.key', runtime='/run/' + roles['proxy']))
        wireguard = (conf / 'wg-nxrs.conf').read_text().replace('/etc/nxrs-monitor/firewall.nft', str(conf / 'firewall.nft'))
        (conf / 'wg-nxrs.conf').write_text(wireguard)
        secret = re.search(r'^PrivateKey = (.+)$', wireguard, re.M).group(1)
        server_public = command(['wg', 'pubkey'], (secret + '\n').encode()).decode().strip()
        private_write(conf / 'wg-phone.conf', '[Interface]\nAddress = 10.77.0.2/32\nMTU = 1380\n'
            f'PrivateKey = {private.decode().strip()}\n\n[Peer]\nPublicKey = {server_public}\n'
            'Endpoint = 192.0.2.1:51820\nAllowedIPs = 10.77.0.1/32\nPersistentKeepalive = 25\n')
        # Unlike earlier tests this dependency actually owns the WireGuard interface.
        units = strict_vpn.services(include_ingest=True)
        units['wg-quick@wg-nxrs.service'] = (
            '[Unit]\nRequires=nxrs-firewall.service\nAfter=nxrs-firewall.service\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            f'ExecStart=/usr/bin/wg-quick up {conf}/wg-nxrs.conf\n'
            f'ExecStop=/usr/bin/wg-quick down {conf}/wg-nxrs.conf\n')
        for original, text in units.items():
            name, text = adapt_unit(text, original, prefix, root, nets.server)
            if original in [f'nxrs-{r}.service' for r in ROLES]:
                role = original[5:-8]
                private_write(conf / (role + '.log'), '')
                text += f'\n[Service]\nStandardOutput=append:{conf}/{role}.log\nStandardError=append:{conf}/{role}.log\n'
            path = runtime_units / name
            require(not path.exists(), 'new runtime unit required')
            path.write_text(text)
            unit_files.append(path)
            names.append(name)
        command(['systemctl', 'daemon-reload'])
        private_write(conf / 'phone.json', json.dumps({'origin': 'https://10.77.0.1:8443',
            'ca': str(conf / 'tls/fullchain.pem'), 'viewers': {
                'a': (conf / 'viewer.token').read_text().strip(), 'b': second}}))
        stage = 'startup'
        await start()
        nets.inside(nets.phone, 'wg-quick', 'up', conf / 'wg-phone.conf')
        phone = await Phone.start(nets.phone, conf / 'phone.json')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            async def internal(method, path, body=None):
                return await internal_request(http, method, path, body)

            async def ready():
                try:
                    status, body = await internal('GET', '/internal/viewers/viewer_other/revocation')
                    return status == 200 and body['media_control_ready'] and prop('ingest', 'MainPID') != '0'
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return False

            async def fresh():
                nonlocal stage
                recovery = stage
                stage = recovery + ': control readiness'
                await until(ready, 'complete group ready')
                # Separately wait for actual publication, never infer readiness from active units.
                async def publishing():
                    async with http.get('http://127.0.0.1:9997/v3/paths/get/reference',
                        headers={'Authorization': 'Bearer ' + security['media_api_token']}) as response:
                        return response.status == 200 and (await response.json()).get('ready') is True
                stage = recovery + ': source publication'
                await until(publishing, 'actual source publication ready')
                stage = recovery + ': verified HTTPS over VPN'
                require((await phone.ask('health')) == {'status': 200, 'healthy': True}, 'HTTPS health over VPN')
                stage = recovery

            async def frozen(reader_id):
                await asyncio.sleep(1.5)
                before = await phone.ask('count', id=reader_id)
                await asyncio.sleep(1)
                require(await phone.ask('count', id=reader_id) == before, 'decoded video must stop')

            async def source_released():
                return (await camera.command('status'))['active'] == 0

            await fresh()
            for role in ROLES:
                pid = int(prop(role, 'MainPID'))
                fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                require(int(fields['Uid'].split()[0]) != 0 and int(fields['CapEff'].strip(), 16) == 0
                        and fields['NoNewPrivs'].strip() == '1', 'unprivileged generated services')
                require(os.stat(f'/proc/{pid}/ns/net').st_ino == os.stat('/run/netns/' + nets.server).st_ino,
                        'service in intended namespace')
            a = await phone.ask('connect', viewer='a')
            b = await phone.ask('connect', viewer='b')
            counters = await camera.command('status')
            require(counters['connections'] == 1 and counters['peak'] == 1, 'one admitted camera connection')
            group = Path('/sys/fs/cgroup') / prop('ingest', 'ControlGroup').lstrip('/')
            require(len(cgroup_pids(group)) == 3, 'ingest, encoder and publisher in shared cgroup')
            publisher_token = (conf / 'publisher.token').read_bytes().strip()
            for pid in cgroup_pids(group):
                for field in ['cmdline', 'environ']:
                    require(publisher_token not in Path(f'/proc/{pid}/{field}').read_bytes(),
                            'no publisher credential in actual process arguments/environment')
            print('PASS: ingest, encoder and SDK publisher share the bounded cgroup without credential argv/environment',flush=True)
            handshakes = command(['wg', 'show', 'wg-nxrs', 'latest-handshakes']).split()
            require(handshakes and int(handshakes[-1]) > 0, 'authenticated kernel WireGuard handshake')
            print('PASS: full strict group delivers two advancing 320x240 H264 streams over kernel WireGuard from one actual Rust source', flush=True)
            stage = 'revocation'
            status, _ = await internal('POST', '/internal/viewers/viewer_reference/revoke', {})
            require(status == 202, 'durable selective revocation accepted')
            await frozen(a['id'])
            before = (await phone.ask('count', id=b['id']))['count']
            await asyncio.sleep(.8)
            require((await phone.ask('count', id=b['id']))['count'] > before, 'other viewer continues')
            require((await phone.ask('catalog', viewer='a'))['status'] == 403, 'revoked account denied through VPN')
            print('PASS: application revocation stops only its viewer while camera, encoder, VPN and authorized viewer continue', flush=True)
            stage = 'controller crash'
            previous=handler_epoch()
            os.kill(int(prop('control', 'MainPID')), signal.SIGKILL)
            await until(lambda: handler_finished(previous),'controller crash joins the owned failure handler')
            await until(stopped, 'controller crash stops all four real services')
            require(not cgroup_pids(group), 'controller loss empties ingest cgroup')
            await until(source_released, 'controller loss releases actual camera')
            await frozen(b['id'])
            require(command(['systemctl', 'is-active', network], check=False).strip() == b'active',
                    'video stopped by service supervision, not VPN teardown')
            print('PASS: controller SIGKILL stops media/proxy/ingest, reaps encoder and releases source while VPN remains up', flush=True)
            stage = 'durable recovery'
            await start()
            await fresh()
            require((await phone.ask('catalog', viewer='a'))['status'] == 403, 'denial survives full group restart')
            c = await phone.ask('connect', viewer='b')
            require(c['count'] >= 5, 'authorized decoded recovery')
            print('PASS: explicit full-group recovery preserves revocation and restores authorized video across the same tunnel', flush=True)
            stage = 'VPN dependency stop'
            command(['systemctl', 'stop', network])
            await until(stopped, 'real VPN service stop propagates to entire group')
            require(not cgroup_pids(group), 'VPN stop empties encoder cgroup')
            await until(source_released, 'VPN stop releases actual camera')
            await frozen(c['id'])
            require('wg-nxrs' not in command(['ip', '-o', 'link', 'show']).decode(), 'real WireGuard interface removed')
            # Rules persist after shutdown instead of exposing the tunnel-address services.
            command(['nft', 'list', 'table', 'inet', 'nxrs_monitor'])
            print('PASS: actual WireGuard-service stop removes the tunnel and stops every video service and camera reader', flush=True)
            stage = 'VPN recovery'
            await start()
            await fresh()
            d = await phone.ask('connect', viewer='b')
            require((await phone.ask('catalog', viewer='a'))['status'] == 403, 'VPN restart cannot restore revoked account')
            require((await phone.ask('delete', id=d['id']))['deleted'], 'WHEP deletion through recovered tunnel')
            print('PASS: explicit VPN/group recovery restores decoded video and WHEP DELETE without resetting identity', flush=True)
    except Exception as error:
        print('COMBINED CHECKPOINT:', stage, flush=True)
        print('COMBINED FAILURE CATEGORY:', type(error).__name__, flush=True)
        # Fixed fields only; never print raw service logs, argv, response bodies or key material.
        for role in ROLES:
            fields = {}
            for key in ['ActiveState', 'SubState', 'Result', 'ExecMainStatus']:
                try:
                    value = prop(role, key)
                    fields[key] = value if re.fullmatch(r'[A-Za-z0-9_-]{0,64}', value) else 'redacted'
                except Exception:
                    fields[key] = 'unavailable'
            print('COMBINED SERVICE:', role, json.dumps(fields), flush=True)
        raise
    finally:
        failures = []
        if phone is not None:
            try:
                await phone.close()
            except Exception:
                failures.append('phone')
        if names:
            failures += cleanup_all([('stop units', lambda: command(['systemctl', 'stop', *names], check=False)),
                                     ('reset units', lambda: command(['systemctl', 'reset-failed', *names], check=False))])
        if camera is not None:
            try:
                await camera.close()
            except Exception:
                failures.append('camera')
        failures += cleanup_all([('unit file', lambda p=p: p.unlink(missing_ok=True)) for p in unit_files])
        failures += cleanup_all([('reload', lambda: command(['systemctl', 'daemon-reload']))])
        for path in [state, root]:
            failures += cleanup_all([('private fixture', lambda p=path: shutil.rmtree(p) if p.exists() else None)])
        if account_created:
            failures += cleanup_all([('account', lambda: command(['userdel', roles['control']]))])
        require(not failures, 'owned combined fixture cleanup incomplete')
    print('COMBINED REFERENCE PASS: actual Rust stream service with generated images; no physical camera/iPhone/cellular qualification', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['gateway', 'ingest', 'publisher', 'mediamtx', 'camera-source','proxy-launcher','proxy-review']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    prerequisites()  # Before any privileged mutation.
    for name in ['gateway', 'ingest', 'publisher', 'mediamtx', 'camera_source','proxy_launcher','proxy_review']:
        setattr(args, name, getattr(args, name).resolve(strict=True))
    prefix = 'nxrs-full-' + secrets.token_hex(4)
    nets = Networks(prefix)
    try:
        nets.setup()  # Enter namespace BEFORE creating the asyncio loop and its sockets.
        asyncio.run(exercise(args, prefix, nets))
    finally:
        nets.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('COMBINED REFERENCE FAILED (private diagnostics suppressed)', flush=True)
        raise SystemExit(1) from None
