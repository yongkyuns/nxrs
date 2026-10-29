#!/usr/bin/env python3
"""Disposable hosted Linux LUKS2/MediaMTX/restic qualification; no physical claim."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import recording_service as recording
import strict_vpn


def require(value, message):
    if not value:
        raise AssertionError(message)


def command(args, *, check=True, timeout=30, **kwargs):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=timeout, **kwargs)
    if check:
        tool = Path(str(args[0])).name
        require(result.returncode == 0, f'owned {tool} operation failed ({result.returncode})')
    return result


def until(predicate, message, seconds=20):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(message)


def exercise(gateway, media, publisher):
    require(os.geteuid() == 0 and sys.platform == 'linux'
        and os.environ.get('GITHUB_ACTIONS') == 'true'
        and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
        and Path('/proc/1/comm').read_text().strip() == 'systemd', 'disposable hosted Linux root required')
    prefix = 'nxrs-rec-'+secrets.token_hex(4)
    root = Path('/run')/prefix
    require(not root.exists(), 'new owned fixture required')
    root.mkdir(mode=0o755)
    store, backup, keys = [root/name for name in ['store', 'backup', 'keys']]
    for path in [store, backup]:
        path.mkdir(mode=0o755)
    keys.mkdir(mode=0o700)
    source_id = str(uuid.uuid4())
    mapper = prefix
    source_mount = command(['systemd-escape', '--path', '--suffix=mount', store]).stdout.decode().strip()
    backup_mount = command(['systemd-escape', '--path', '--suffix=mount', backup]).stdout.decode().strip()
    crypt = command(['systemd-escape', '--template=systemd-cryptsetup@.service', mapper]).stdout.decode().strip()
    roles = {name: prefix+'-'+name for name in ['media', 'volume', 'backup', 'backup-init', 'network']}
    restore_template = prefix+'-restore@.service'
    repo = backup/'repository'
    helper = root/'volume-check'
    created, accounts, loops, children = [], [], [], []
    previous_core = Path('/proc/sys/kernel/core_pattern').read_text()
    previous_swaps = [row.split()[0] for row in Path('/proc/swaps').read_text().splitlines()[1:]]
    private_key, password = secrets.token_bytes(64), secrets.token_hex(32).encode()
    for path, value in [(keys/'recordings.key', private_key), (keys/'backup.password', password)]:
        path.write_bytes(value); path.chmod(0o600)

    def adapt(text):
        for old, new in [(recording.CRYPT, crypt), (recording.MOUNT, source_mount),
            (recording.BACKUP_MOUNT, backup_mount), (recording.CHECK, roles['volume']+'.service'),
            (recording.ROOT, str(store)), ('/var/lib/nxrs-backup', str(backup)),
            ('/run/nxrs-keys', str(keys)), ('/usr/local/libexec/nxrs-recording-volume', str(helper)),
            ('nxrs-restore@', prefix+'-restore@'), ('nxrs-backup-init', roles['backup-init']),
            ('nxrs-backup', roles['backup']), ('nxrs-media', roles['media']), ('nxrs-recordings', mapper)]:
            text = text.replace(old, new)
        return text

    def install_unit(name, text):
        path = Path('/run/systemd/system')/name
        require(not path.exists(), 'new owned unit required')
        path.write_text(text); created.append(path)

    def unit(name, action, *, check=True):
        return command(['systemctl', action, name], check=check, timeout=60)

    def property(name, key):
        return command(['systemctl', 'show', name, '-p', key, '--value']).stdout.decode().strip()

    def new_loop(name, size):
        path = root/name
        with path.open('xb') as output:
            output.truncate(size)
        device = command(['losetup', '--find', '--show', path]).stdout.decode().strip()
        require(device.startswith('/dev/loop') and device[9:].isdigit(), 'owned loop device')
        loops.append(device)
        return device

    def decode(paths):
        frames = 0
        for path in paths:
            result = command(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,nb_read_frames', '-of', 'json', path])
            stream = json.loads(result.stdout)['streams'][0]
            require((stream['width'], stream['height']) == (320, 240), 'recorded dimensions')
            frames += int(stream['nb_read_frames'])
            command(['ffmpeg', '-nostdin', '-v', 'error', '-i', path, '-f', 'null', '-'])
        return frames

    def restic(*args, check=True):
        return command(['restic', '--repo', repo, '--password-file', keys/'backup.password',
            '--no-cache', *args], check=check, env={**os.environ, 'TMPDIR': str(store/'cache/tmp')}, timeout=60)

    try:
        print('RECORDING STAGE: owned volume provisioning', flush=True)
        command(['swapoff', '-a'])
        Path('/proc/sys/kernel/core_pattern').write_text('|/bin/false\n')
        config = root/'bundle'
        strict_vpn.generate(config, 'camera.test', '10.77.0.1', '10.77.0.2', 'endpoint.test',
            base64.b64encode(secrets.token_bytes(32)).decode(), recording_volume=source_id)
        (root/'bin').mkdir(mode=0o755)
        shutil.copyfile(media, root/'bin/mediamtx'); (root/'bin/mediamtx').chmod(0o755)
        (root/'accounts').write_text(adapt((config/'nxrs-recording.sysusers').read_text()))
        for account in [roles['media'], roles['backup']]:
            try:
                pwd.getpwnam(account)
            except KeyError:
                pass
            else:
                raise AssertionError('new fixture accounts required')
        command(['systemd-sysusers', root/'accounts']); accounts += [roles['media'], roles['backup']]
        gid = grp.getgrnam(mapper).gr_gid
        source = new_loop('source.img', 128*1024*1024)
        destination = new_loop('backup.img', 128*1024*1024)
        command(['cryptsetup', 'luksFormat', '--batch-mode', '--type', 'luks2', '--uuid', source_id,
            '--cipher', 'aes-xts-plain64', '--key-size', '512', '--pbkdf-memory', '32768',
            '--pbkdf-parallel', '1', '--iter-time', '100', '--key-file', keys/'recordings.key', source])
        command(['cryptsetup', 'open', '--key-file', keys/'recordings.key', source, mapper])
        command(['mkfs.ext4', '-q', '/dev/mapper/'+mapper])
        command(['mount', '/dev/mapper/'+mapper, store])
        os.chmod(store, 0o750); os.chown(store, 0, gid)
        for name, owner, mode in [('media', roles['media'], 0o750), ('media/.tmp', roles['media'], 0o750),
            ('cache', roles['backup'], 0o700), ('cache/tmp', roles['backup'], 0o700),
            ('cache/restic', roles['backup'], 0o700), ('restore', roles['backup'], 0o700)]:
            path = store/name; path.mkdir(mode=mode)
            os.chown(path, pwd.getpwnam(owner).pw_uid, gid)
        command(['umount', store]); command(['cryptsetup', 'close', mapper])
        command(['udevadm', 'trigger', '--action=change', '/sys/class/block/'+Path(source).name])
        command(['udevadm', 'settle', '--timeout=10'])
        require(Path('/dev/disk/by-uuid', source_id).exists(), 'actual LUKS UUID discovery')
        command(['mkfs.ext4', '-q', destination])
        command(['mount', '-o', 'nodev,nosuid,noexec,noatime', destination, backup])
        repo.mkdir(mode=0o700); os.chown(repo, pwd.getpwnam(roles['backup']).pw_uid, gid)
        helper.write_text(adapt((config/'nxrs-recording-volume').read_text())); helper.chmod(0o755)
        generator = root/'generator'; generator.mkdir()
        (root/'crypttab').write_text(adapt((config/'nxrs-recordings.crypttab').read_text()))
        command(['/usr/lib/systemd/system-generators/systemd-cryptsetup-generator', generator],
            env={**os.environ, 'SYSTEMD_CRYPTTAB': str(root/'crypttab')})
        install_unit(crypt, (generator/crypt).read_text())
        install_unit(source_mount, adapt((config/recording.MOUNT).read_text()))
        install_unit(roles['volume']+'.service', adapt((config/recording.CHECK).read_text()))
        install_unit(roles['network']+'.service', '[Service]\nType=oneshot\nExecStart=/usr/bin/true\nRemainAfterExit=yes\n')
        for name in ['nxrs-backup-init.service', 'nxrs-backup.service', 'nxrs-restore@.service']:
            install_unit(adapt(name), adapt((config/name).read_text()))
        # Only network/controller dependencies and installed binary/config paths are adapted.
        ports = []
        for _ in range(4):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); ports.append(sock.getsockname()[1])
        control = json.loads((config/'config.json').read_text())
        control.update(listen=f'127.0.0.1:{ports[0]}', auth_listen=f'127.0.0.1:{ports[1]}')
        (config/'config.json').write_text(json.dumps(control))
        state = root/'state'; state.mkdir(mode=0o700)
        strict_vpn.initialize_state(state)
        security = json.loads((config/'security.json').read_text())
        security.update(revocations_file=str(state/'revocations.json'), media_api=f'127.0.0.1:{ports[3]}')
        (config/'security.json').write_text(json.dumps(security))
        media_config = json.loads(adapt((config/'mediamtx.json').read_text()))
        media_config.update(authHTTPAddress=f'http://127.0.0.1:{ports[1]}/internal/mediamtx/auth',
            rtspAddress=f'127.0.0.1:{ports[2]}', apiAddress=f'127.0.0.1:{ports[3]}', webrtc=False)
        (config/'mediamtx.json').write_text(json.dumps(media_config))
        text = (config/'nxrs-media.service').read_text().replace(
            '/usr/local/libexec/nxrs-mediamtx', str(root/'bin/mediamtx'))
        text = adapt(text)
        for dependency in ['nxrs-control.service', 'wg-quick@wg-nxrs.service', 'nxrs-firewall.service']:
            text = text.replace(dependency, roles['network']+'.service')
        text = text.replace('nxrs-monitor.target', roles['network']+'.service')
        text = text.replace('/etc/nxrs-monitor/mediamtx.json', str(config/'mediamtx.json'))
        require('ExecStart='+str(root/'bin/mediamtx')+' %d/config\n' in text,
            'fixture role substitution preserves the real media executable')
        install_unit(roles['media']+'.service', text)
        command(['systemctl', 'daemon-reload'])
        verification = command(['systemd-analyze', 'verify', *created], check=False)
        if verification.returncode:
            diagnostic = verification.stderr[-8192:].decode(errors='replace').lower()
            print('RECORDING UNIT CHECK:', json.dumps({category: diagnostic.count(category) for category in
                ['not executable', 'no such file', 'not found', 'failed to parse', 'unknown key',
                 'ordering cycle', 'conflicting', 'specifier', 'unbalanced', 'invalid']}), flush=True)
        require(verification.returncode == 0, 'generated unit verification')
        child = subprocess.Popen([str(gateway), str(config/'config.json'), '--security', str(config/'security.json')],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(child)
        print('RECORDING STAGE: real cryptsetup unit key refusals', flush=True)
        keys.joinpath('recordings.key').write_bytes(secrets.token_bytes(64))
        require(unit(roles['media']+'.service', 'start', check=False).returncode != 0, 'wrong volume key refused')
        keys.joinpath('recordings.key').unlink()
        command(['systemctl', 'reset-failed', crypt, source_mount, roles['volume']+'.service', roles['media']+'.service'], check=False)
        require(unit(roles['media']+'.service', 'start', check=False).returncode != 0, 'missing volume key refused')
        keys.joinpath('recordings.key').write_bytes(private_key); keys.joinpath('recordings.key').chmod(0o600)
        command(['systemctl', 'reset-failed', crypt, source_mount, roles['volume']+'.service', roles['media']+'.service'], check=False)
        unit(roles['media']+'.service', 'start')
        require(property(roles['volume']+'.service', 'ActiveState') == 'active', 'actual encrypted volume admitted')
        require(command(['runuser', '-u', 'nobody', '--', 'ls', store/'media'], check=False).returncode != 0,
            'unrelated account cannot list footage')
        payload = command(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=320x240:rate=10', '-frames:v', '120', '-an', '-c:v', 'libx264',
            '-bf', '0', '-g', '10', '-f', 'mpegts', 'pipe:1']).stdout
        token = (config/'publisher.token').read_bytes().strip()

        def ready():
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{ports[1]}/internal/viewers/viewer_reference/revocation', timeout=1) as response:
                    return json.loads(response.read(1025)).get('media_control_ready') is True
            except OSError:
                return False

        def publish(accepted=True):
            until(ready, 'recording media authorization ready')
            child = subprocess.Popen([str(publisher), f'127.0.0.1:{ports[2]}', 'publisher_reference', 'reference'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            children.append(child)
            try:
                child.stdin.write(token); child.stdin.flush()
                chunk = (len(payload)+119)//120
                for offset in range(0, len(payload), chunk):
                    child.stdin.write(payload[offset:offset+chunk]); child.stdin.flush()
                    time.sleep(.1)
            except BrokenPipeError:
                pass
            finally:
                try:
                    child.stdin.close()
                except BrokenPipeError:
                    pass
                child.stdin = None
            child.communicate(timeout=10)
            if accepted:
                require(child.returncode == 0, 'private-pipe SDK publication')

        print('RECORDING STAGE: native recording and decoded playback', flush=True)
        publish(); unit(roles['media']+'.service', 'stop')
        clips = list((store/'media').rglob('*.mp4'))
        require(clips and decode(clips) == 120, 'all published frames decode from completed recording')
        hashes = {path.relative_to(store/'media'): hashlib.sha256(path.read_bytes()).hexdigest() for path in clips}
        auxiliary = secrets.token_bytes(16*1024*1024)
        (store/'media/metadata.bin').write_bytes(auxiliary)
        (store/'media/metadata.bin').chmod(0o640)
        os.chown(store/'media/metadata.bin', pwd.getpwnam(roles['media']).pw_uid, gid)
        print('RECORDING STAGE: native backup enrollment and credential refusal', flush=True)
        unit(roles['backup-init']+'.service', 'start')
        require(unit(roles['backup-init']+'.service', 'start', check=False).returncode != 0, 'existing repository never reinitialized')
        unit(roles['backup']+'.service', 'start')
        snapshots = json.loads(restic('snapshots', '--json').stdout)
        require(len(snapshots) == 1, 'one complete authenticated snapshot')
        snapshot = snapshots[0]['id']; require(len(snapshot) == 64, 'bounded snapshot ID')
        keys.joinpath('backup.password').write_bytes(secrets.token_hex(32).encode())
        require(unit(roles['backup']+'.service', 'start', check=False).returncode != 0, 'wrong backup password refused')
        keys.joinpath('backup.password').write_bytes(password)
        instance = prefix+'-restore@'+snapshot+'.service'
        template = Path('/run/systemd/system')/restore_template
        normal_restore = template.read_text()
        template.write_text(normal_restore.replace(' restore %i:', ' --limit-download 1024 restore %i:'))
        command(['systemctl', 'daemon-reload'])
        print('RECORDING STAGE: interrupted isolated restore and exact recovery', flush=True)
        command(['systemctl', 'start', '--no-block', instance])
        stage = store/'restore'/snapshot

        def partially_restored():
            path = stage/'metadata.bin'
            return path.exists() and path.stat().st_size > 0 and path.read_bytes()[:4096] == auxiliary[:4096] \
                and hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(auxiliary).digest() \
                and int(property(instance, 'MainPID')) > 1

        until(partially_restored, 'observe actual partial restore before interruption', seconds=30)
        command(['systemctl', 'kill', '--kill-whom=main', '--signal=KILL', instance])
        until(lambda: property(instance, 'ActiveState') == 'failed', 'interrupted restore stops')
        require(unit(instance, 'start', check=False).returncode != 0, 'partial restore target is never reused')
        stage.rename(stage.with_name(snapshot+'.interrupted'))
        template.write_text(normal_restore); command(['systemctl', 'daemon-reload'])
        unit(instance, 'start')
        require(decode(list(stage.rglob('*.mp4'))) == 120, 'restored video independently decodes')
        for relative, digest in hashes.items():
            require(hashlib.sha256((stage/relative).read_bytes()).hexdigest() == digest, 'exact restored clip bytes')
        require((stage/'metadata.bin').read_bytes() == auxiliary, 'metadata restored exactly')
        print('RECORDING STAGE: ciphertext integrity refusal', flush=True)
        pack = max((repo/'data').rglob('*'), key=lambda p: p.stat().st_size if p.is_file() else -1)
        original = pack.read_bytes(); damaged = bytearray(original); damaged[len(damaged)//2] ^= 1
        pack.write_bytes(damaged)
        require(restic('check', '--read-data', check=False).returncode != 0, 'corrupt ciphertext detected')
        pack.write_bytes(original)
        print('RECORDING STAGE: bounded capacity and explicit recovery', flush=True)
        unit(roles['media']+'.service', 'start')
        fill = store/'media/fill'
        try:
            with fill.open('xb') as output:
                while True:
                    output.write(bytes(65536))
        except OSError as error:
            require(error.errno == 28, 'only the owned recording filesystem reaches capacity')
        publish(accepted=False); fill.unlink()
        unit(roles['media']+'.service', 'stop'); unit(roles['media']+'.service', 'start')
        before = set((store/'media').rglob('*.mp4'))
        publish(); unit(roles['media']+'.service', 'stop')
        recovered = set((store/'media').rglob('*.mp4'))-before
        require(recovered and decode(recovered) == 120, 'recording recovers after volume capacity returns')
        print('RECORDING STAGE: accelerated native retention', flush=True)
        media_config['paths']['reference']['recordDeleteAfter'] = '3s'  # Fixture-only accelerated native retention.
        media_config['paths']['reference']['recordSegmentDuration'] = '1s'
        (config/'mediamtx.json').write_text(json.dumps(media_config))
        unit(roles['media']+'.service', 'start')
        until(lambda: not list((store/'media').rglob('*.mp4')), 'native retention removes expired segments', seconds=90)
        unit(source_mount, 'stop')
        until(lambda: property(roles['media']+'.service', 'ActiveState') == 'inactive', 'unmount retires recording service')
        require(not (store/'media').exists(), 'no plaintext fallback store')
        print('RECORDING STAGE: actual mount identity and locked-volume restart', flush=True)
        # Leave the genuine approved mapper active, but substitute a plain ext4 mount.
        command(['mount', destination, store])
        require(command([helper, source_id], check=False).returncode != 0, 'plain mounted source refused')
        mutant = root/'binding-control'
        mutant.write_text(helper.read_text().replace('test "$(cat "/sys/dev/block/$device/dm/uuid")" = "$expected"\n', ''))
        mutant.chmod(0o755)
        require(command([mutant, source_id], check=False).returncode == 0, 'binding refusal has a live positive control')
        command(['umount', store]); unit(crypt, 'stop')
        media_config['paths']['reference'].update(recordDeleteAfter='24h', recordSegmentDuration='1m')
        (config/'mediamtx.json').write_text(json.dumps(media_config))
        unit(roles['media']+'.service', 'start')
        require((store/'media/metadata.bin').read_bytes() == auxiliary, 'recording state survives locked-volume restart')
        publish(); unit(roles['media']+'.service', 'stop')
        require(decode(list((store/'media').rglob('*.mp4'))) == 120, 'fresh recording after remount')
        print('RECORDING PASS: actual LUKS2 admission/key refusals, native recording/retention/capacity/restart; '
            'restic backup, wrong-key/corruption refusal, interrupted isolated restore and exact decoded recovery; hosted only', flush=True)
    except BaseException:
        # Structural facts only: no raw child logs, credentials or footage.
        facts = {'swap_disabled': len(Path('/proc/swaps').read_text().splitlines()) == 1,
                 'core_discard': Path('/proc/sys/kernel/core_pattern').read_text().strip() == '|/bin/false'}
        for role, name in [('crypto', crypt), ('volume', roles['volume']+'.service'),
                           ('media', roles['media']+'.service'), ('backup', roles['backup']+'.service')]:
            try:
                state = property(name, 'ActiveState')
                result = property(name, 'Result')
                status = property(name, 'ExecMainStatus')
                facts[role] = {'state': state if state in ['active', 'inactive', 'failed', 'activating', 'deactivating'] else 'other',
                    'result': result if result in ['success', 'exit-code', 'signal', 'core-dump', 'timeout', 'resources', 'oom-kill', 'start-limit-hit', 'dependency'] else 'other',
                    'exit_code': int(status) if status.isdigit() and len(status) < 10 else None}
            except Exception:
                facts[role] = 'unavailable'
        if helper.exists() and (store/'media').exists():
            facts['direct_volume_check'] = command([helper, source_id], check=False).returncode == 0
        print('RECORDING DIAGNOSTIC:', json.dumps(facts, sort_keys=True), flush=True)
        raise
    finally:
        failures = []
        for child in children:
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            except Exception:
                child.kill(); child.wait(timeout=5)
        for path in reversed(created):
            if path.name.endswith('@.service'):
                command(['systemctl', 'stop', path.stem[:-1]+'*.service'], check=False)
            else:
                unit(path.name, 'stop', check=False)
        for path in [store, backup]:
            if command(['findmnt', '-rn', '-M', path], check=False).returncode == 0:
                if command(['umount', path], check=False).returncode != 0:
                    failures.append('owned mount')
        command(['cryptsetup', 'close', mapper], check=False)
        if Path('/dev/mapper', mapper).exists():
            failures.append('owned crypto mapping')
        for device in loops:
            if command(['losetup', '--detach', device], check=False).returncode != 0:
                failures.append('owned loop')
        for path in created:
            path.unlink(missing_ok=True)
        command(['systemctl', 'daemon-reload'])
        for account in accounts:
            command(['userdel', account], check=False)
        command(['groupdel', mapper], check=False)
        Path('/proc/sys/kernel/core_pattern').write_text(previous_core)
        for source in previous_swaps:
            command(['swapon', source])
        require(not failures, 'recording fixture cleanup failed')
        shutil.rmtree(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['gateway', 'mediamtx', 'publisher']:
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    try:
        exercise(args.gateway.resolve(), args.mediamtx.resolve(), args.publisher.resolve())
    except Exception as error:
        print('RECORDING FAILED:', str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
