#!/usr/bin/env python3
"""Qualify only a named, disposable AVD. Trust is scoped to test transport;
private configuration travels through stdin and all owned forwards are retired.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def main(path):
    sdk = Path(os.environ['ANDROID_HOME'])
    serial = os.environ['ANDROID_SERIAL']
    if not serial.startswith('emulator-'):
        raise AssertionError('explicit test-owned emulator required')
    adb = [os.environ.get('NXRS_ADB', str(sdk/'platform-tools/adb')), '-s', serial]

    def call(arguments, timeout=180, **kwargs):
        return subprocess.run(adb + arguments, check=True, timeout=timeout, **kwargs)

    call(['wait-for-device'], stdout=subprocess.DEVNULL)
    name = call(['emu', 'avd', 'name'], capture_output=True).stdout.decode().splitlines()[0]
    if not name.startswith('nxrs-multicamera-'):
        raise AssertionError('AVD name must start with nxrs-multicamera-')
    # ADB connects before Android has mounted the app's credential-encrypted
    # storage. Wait for boot and unlock before installing/delivering fixtures.
    deadline = time.monotonic() + 120
    def wait_for(arguments, expected):
        while time.monotonic() < deadline:
            result = call(['shell'] + arguments, timeout=10, capture_output=True)
            if result.stdout.decode().strip() == expected:
                return
            time.sleep(.2)
        raise AssertionError('owned emulator boot/unlock deadline')
    wait_for(['getprop', 'sys.boot_completed'], '1')
    call(['shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'], stdout=subprocess.DEVNULL)
    call(['shell', 'wm', 'dismiss-keyguard'], stdout=subprocess.DEVNULL)
    wait_for(['am', 'get-started-user-state', '0'], 'RUNNING_UNLOCKED')
    print('ANDROID QUALIFICATION: boot complete; test user unlocked', flush=True)
    config = json.loads(path.read_text())
    forwards = []
    display = {}
    installed = False
    marker = None
    metrics = None
    with tempfile.TemporaryDirectory(prefix='nxrs-android-run-') as directory:
        root = Path(directory)
        root.chmod(0o700)
        with (root/'private.log').open('wb') as log:
            try:
                for port in config['ports']:
                    call(['reverse', f'tcp:{port}', f'tcp:{port}'], stdout=log, stderr=log)
                    forwards.append(port)
                for apk in ['app-debug.apk', 'app-debug-androidTest.apk']:
                    variant = 'androidTest/debug' if 'androidTest' in apk else 'debug'
                    artifact = ROOT/'clients/android/app/build/outputs/apk'/variant/apk
                    call(['install', '-r', str(artifact)], stdout=log, stderr=log)
                    installed = True
                # Physical skin pixels do not define Android's logical density.
                # Keep both camera cards and their real controls visible in this
                # owned fixture, then restore the previous overrides on exit.
                for field, value in [('size', '720x1280'), ('density', '160')]:
                    before = call(['shell', 'wm', field], capture_output=True).stdout.decode()
                    previous = re.search(r'Override ' + field + r':\s*(\S+)', before)
                    display[field] = previous.group(1) if previous else 'reset'
                    call(['shell', 'wm', field, value], stdout=log, stderr=log)
                call(['shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'], stdout=log, stderr=log)
                call(['shell', 'wm', 'dismiss-keyguard'], stdout=log, stderr=log)
                print('ANDROID QUALIFICATION: owned viewport 720x1280 at 160 dpi', flush=True)
                payload = {key: config[key] for key in [
                    'gateway', 'mediaOrigin', 'token', 'rootCertificateBase64',
                    'qualificationCase', 'minimumFPS']}
                call(['shell', 'run-as', 'org.nxrs.monitor', 'sh', '-c',
                      "'umask 077; mkdir -p cache; cat > cache/qualification.json'"],
                     input=json.dumps(payload).encode(), stdout=log, stderr=log)
                result = subprocess.run(adb + ['shell', 'am', 'instrument', '-w', '-r',
                    '-e', 'class', 'org.nxrs.monitor.CameraQualificationTest,org.nxrs.monitor.RenderLifetimeTest,org.nxrs.monitor.CatalogLifetimeTest',
                    'org.nxrs.monitor.test/androidx.test.runner.AndroidJUnitRunner'],
                    capture_output=True, timeout=180)
                output = result.stdout.decode(errors='replace')
                log.write(result.stdout)
                log.write(result.stderr)
                log.flush()
                markers = [line for line in output.splitlines()
                           if line.startswith('MULTICAMERA ANDROID PASS:')]
                measurements = [line for line in output.splitlines()
                                if line.startswith('MULTICAMERA METRICS:')]
                if (result.returncode or 'FAILURES!!!' in output
                        or not re.search(r'OK \(4 tests\)', output)
                        or len(markers) != 1 or len(measurements) != 1):
                    # Fixed assertion/transport categories only; never dump
                    # credentials, server responses or native SDK diagnostics.
                    print('ANDROID TEST RESULT:', re.findall(
                        r'(?:OK \(\d+ tests?\)|FAILURES!!!|Tests run: [^\n]+)', output), flush=True)
                    for line in output.splitlines():
                        if line.startswith(('java.lang.AssertionError:', '\tat org.nxrs.monitor.',
                                'INSTRUMENTATION_STATUS: stack=', 'INSTRUMENTATION_RESULT: shortMsg=',
                                'CAMERA DIAGNOSTIC:', 'MULTICAMERA METRICS:')):
                            print(line.replace(config['token'], '<redacted>'), flush=True)
                    raise AssertionError('Android native instrumentation failed')
                marker = markers[0]
                metrics = json.loads(measurements[0].split(': ', 1)[1])
            finally:
                failures = []
                if installed:
                    try:
                        call(['shell', 'am', 'force-stop', 'org.nxrs.monitor'], stdout=log, stderr=log)
                    except Exception:
                        failures.append('owned application')
                    try:
                        call(['shell', 'run-as', 'org.nxrs.monitor', 'rm', '-f',
                              'cache/qualification.json'], stdout=log, stderr=log)
                    except Exception:
                        failures.append('private configuration')
                for port in reversed(forwards):
                    try:
                        call(['reverse', '--remove', f'tcp:{port}'], stdout=log, stderr=log)
                    except Exception:
                        failures.append('owned forward')
                for field, previous in reversed(list(display.items())):
                    try:
                        call(['shell', 'wm', field, previous], stdout=log, stderr=log)
                    except Exception:
                        failures.append('owned display')
                if failures:
                    raise AssertionError('Android cleanup failed: ' + ', '.join(sorted(set(failures))))
    if os.environ.get('NXRS_CLIENT_REPORT'):
        Path(os.environ['NXRS_CLIENT_REPORT']).write_text(json.dumps(
            dict(metrics, instrumentation_result='PASS'), indent=2) + '\n')
    print('MULTICAMERA METRICS: ' + json.dumps(metrics), flush=True)
    print(marker, flush=True)


if __name__ == '__main__':
    try:
        main(Path(sys.argv[1]))
    except Exception as error:
        print('ANDROID QUALIFICATION FAILED:',
              str(error) if isinstance(error, AssertionError) else type(error).__name__, flush=True)
        raise SystemExit(1) from None
