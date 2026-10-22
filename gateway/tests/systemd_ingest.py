"""Hosted-only owner of one generated ingest service; all other units are disposable.

Controller/media dependencies are no-op units here. Their real crash behavior is
covered by systemd_reference.py. This tests ingest sandbox/cgroup behavior with
real source, encoder and media processes, not a home deployment or VPN.
"""
import builtins
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'deploy'))
import ingest_service


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def require_hosted():
    require(os.geteuid() == 0 and sys.platform == 'linux', 'hosted Linux root required')
    require(os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'refuse non-hosted machine')
    require(Path('/proc/1/comm').read_text().strip() == 'systemd', 'real systemd required')
    require(Path('/sys/fs/cgroup/cgroup.controllers').exists(), 'unified cgroup hierarchy required')


def command(arguments, check=True):
    result = subprocess.run(arguments, capture_output=True, timeout=20)
    if check and result.returncode:
        raise AssertionError('owned ingest unit command failed')
    return result


class ManagedIngest:
    def __init__(self, root, binary):
        require_hosted()
        self.root = Path(root)
        require(self.root.parent == Path('/run'), 'owned /run fixture required')
        self.name = 'nxrs-ingest-test-' + secrets.token_hex(4)
        self.dependency = self.name + '-dependency'
        self.written = []
        self.group = None
        self.installed = False
        (self.root/'bin').mkdir(mode=0o755)
        shutil.copyfile(binary, self.root/'bin/ingest')
        (self.root/'bin/ingest').chmod(0o755)
        config = json.loads((self.root/'ingest.json').read_text())
        self.camera_tls = config.get('camera_tls') is not None
        if self.camera_tls:
            for field in ['ca_certificate_file','camera_certificate_file','client_certificate_file','client_key_file','crl_file']:
                source=Path(config['camera_tls'][field])
                shutil.copyfile(source,self.root/source.name)
                (self.root/source.name).chmod(0o600)
                config['camera_tls'][field]=f'/run/{self.name}/{source.name}'
        config['publisher_token_file'] = f'/run/{self.name}/publisher.token'
        (self.root/'ingest.json').write_text(json.dumps(config))

    def property(self, key):
        return command(['systemctl', 'show', self.name+'.service', '-p', key, '--value']).stdout.decode().strip()

    # Explicit builtins: the method above reads a systemd property and must not
    # accidentally shadow the Python descriptor decorator during class creation.
    @builtins.property
    def pid(self):
        return int(self.property('MainPID'))

    @builtins.property
    def returncode(self):
        return int(self.property('ExecMainStatus'))

    def start(self):
        if not self.installed:
            # Rename template-owned paths before inserting fixture paths, whose
            # random directory prefix may itself contain the original service name.
            text = ingest_service.service(camera_tls=self.camera_tls).replace('nxrs-ingest', self.name)
            text = text.replace('/etc/nxrs-monitor/ingest.json', str(self.root/'ingest.json'))
            text = text.replace('/etc/nxrs-monitor/publisher.token', str(self.root/'publisher.token'))
            if self.camera_tls:
                for name in ['camera-ca.pem','camera-peer.pem','camera-client.pem','camera-client.key','camera-crl.pem']:
                    text=text.replace('/etc/nxrs-monitor/'+name,str(self.root/name))
            text = text.replace('/usr/local/libexec/nxrs-camera-ingest', str(self.root/'bin/ingest'))
            for unit in ['nxrs-control.service', 'nxrs-media.service', 'wg-quick@wg-nxrs.service',
                         'nxrs-firewall.service', 'nxrs-monitor.target']:
                text = text.replace(unit, self.dependency+'.service')
            log = self.root/'managed-ingest.log'
            log.touch(mode=0o600)
            text += f'\n[Service]\nStandardOutput=append:{log}\nStandardError=append:{log}\n'
            for name, content in [(self.name, text), (self.dependency,
                    '[Service]\nType=oneshot\nExecStart=/usr/bin/true\nRemainAfterExit=yes\n')]:
                destination = Path('/run/systemd/system')/(name+'.service')
                require(not destination.exists(), 'new disposable unit required')
                destination.write_text(content)
                self.written.append(destination)
            command(['systemctl', 'daemon-reload'])
            self.installed = True
        command(['systemctl', 'reset-failed', self.name+'.service'], check=False)
        command(['systemctl', 'start', self.name+'.service'])
        group = self.property('ControlGroup')
        require(group == '/system.slice/'+self.name+'.service', 'owned service control group')
        self.group = Path('/sys/fs/cgroup')/group.lstrip('/')

    def group_pids(self):
        if self.group is None or not (self.group/'cgroup.procs').exists():
            return set()
        return set(map(int, (self.group/'cgroup.procs').read_text().split()))

    def assert_sandbox(self):
        pids = self.group_pids()
        require(self.pid in pids and len(pids) == 2, 'adapter and encoder share exactly one cgroup')
        for pid in pids:
            values = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
            require(int(values['Uid'].split()[0]) != 0 and int(values['CapEff'].strip(), 16) == 0
                    and values['NoNewPrivs'].strip() == '1', 'adapter and encoder are unprivileged')
        for name in ['config.json', 'publisher.token']+(['camera-ca.pem','camera-peer.pem','camera-client.pem','camera-client.key','camera-crl.pem'] if self.camera_tls else []):
            metadata = (Path('/run')/self.name/name).stat()
            require(metadata.st_mode & 0o777 == 0o600 and metadata.st_nlink == 1,
                    'staged ingest credentials remain private regular files')
        require(self.property('KillMode') == 'control-group' and self.property('Restart') == 'no',
                'generated cleanup policy remains active')
        require(self.property('MemoryMax') == '268435456' and self.property('TasksMax') == '128',
                'encoder shares service memory and task bounds')

    def terminate(self):
        command(['systemctl', 'stop', self.name+'.service'])

    def wait(self, timeout=10):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.pid == 0 and not self.group_pids():
                return self.returncode
            time.sleep(.05)
        raise AssertionError('owned ingest group did not stop')

    def kill_main(self):
        pid = self.pid
        require(pid > 1, 'running owned ingest process required')
        os.kill(pid, signal.SIGKILL)

    def stop_dependency(self):
        command(['systemctl', 'stop', self.dependency+'.service'])

    def close(self):
        failures = []
        for name in [self.name, self.dependency]:
            try:
                command(['systemctl', 'stop', name+'.service'], check=False)
                command(['systemctl', 'reset-failed', name+'.service'], check=False)
            except Exception:
                failures.append('stop')
        if self.group_pids():
            failures.append('cgroup')
        for path in self.written:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                failures.append('unit file')
        try:
            command(['systemctl', 'daemon-reload'])
        except Exception:
            failures.append('reload')
        require(not failures, 'owned ingest service cleanup incomplete')
