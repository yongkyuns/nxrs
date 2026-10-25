#!/usr/bin/env python3
"""Build and admit the pinned shared Python/native media fixture environment."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import urllib.request

import python_environment as packages

ROOT = packages.ROOT
INPUTS = packages.INPUTS
FFMPEG = 'https://ffmpeg.org/security.html'
OPENSSL = 'https://openssl-library.org/news/vulnerabilities-4.0/'
require = packages.require
Refusal = packages.Refusal


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inputs():
    return {name: digest(ROOT / name) for name in INPUTS}


def fetch(url, limit=2 * 1024 * 1024):
    require(url.startswith('https://'), 'https-source')
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'nxrs-native-qualification'}), timeout=30) as response:
        raw = response.read(limit + 1)
    require(len(raw) <= limit, 'source-size')
    return raw


def version(text):
    require(isinstance(text, str) and re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', text), 'native-version-schema')
    parts = tuple(int(n) for n in text.split('.'))
    return parts if len(parts) == 3 else (*parts, 0)


def ffmpeg_findings(raw, selected):
    # This upstream page lists fixes, not complete introduced/affected ranges.
    # Require every published fix on the selected release branch, conservatively.
    text = raw.decode('utf-8')
    branch = '.'.join(str(n) for n in version(selected)[:2])
    sections = re.split(r'<h2>FFmpeg ([0-9]+\.[0-9]+)</h2>', text)
    require(len(sections) > 10 and sections.count(branch) == 1, 'ffmpeg-branch-index')
    body = sections[sections.index(branch) + 1].split('<h2>', 1)[0]
    releases = re.split(r'<h3>([0-9]+\.[0-9]+(?:\.[0-9]+)?)</h3>', body)
    require(len(releases) >= 3 and len(re.findall(r'<h3\b', body)) == (len(releases) - 1) // 2, 'ffmpeg-release-index')
    result = set()
    for release, entries in zip(releases[1::2], releases[2::2]):
        require(version(release)[:2] == version(selected)[:2], 'ffmpeg-release-branch')
        ids = set(re.findall(r'\bCVE-[0-9]{4}-[0-9]{4,}\b', entries))
        require(ids, 'ffmpeg-advisory-entries')
        if version(release) > version(selected):
            result.update(ids)
    return sorted(result)


def openssl_findings(record, selected, identity):
    require(record['dataType'] == 'CVE_RECORD' and record['cveMetadata']['cveId'] == identity
            and record['cveMetadata']['state'] == 'PUBLISHED', 'openssl-record-identity')
    affected = record['containers']['cna']['affected']
    require(isinstance(affected, list) and affected, 'openssl-products')
    result = set()
    for product in affected:
        require(product['vendor'] == 'OpenSSL' and product['product'] == 'OpenSSL'
                and product['defaultStatus'] == 'unaffected', 'openssl-product')
        for item in product['versions']:
            # Old unsupported branches have custom alphabetic release suffixes.
            # This candidate admits only OpenSSL 4.0; 3.5 is a metadata control.
            start = item['version']
            if not start.startswith(('3.', '4.')):
                continue
            require(item['versionType'] == 'semver' and item['status'] == 'affected'
                    and set(item) <= {'version', 'lessThan', 'lessThanOrEqual', 'status', 'versionType'}, 'openssl-range-schema')
            lower = version(start)
            if 'lessThan' in item:
                match = lower <= version(selected) < version(item['lessThan'])
            elif 'lessThanOrEqual' in item:
                match = lower <= version(selected) <= version(item['lessThanOrEqual'])
            else:
                # An unfixed range cannot be treated as an exact version match.
                match = version(selected) >= lower
            if match:
                result.add(identity)
    return sorted(result)


def audit_native(observation):
    ff = fetch(FFMPEG)
    require(not ffmpeg_findings(ff, observation['ffmpeg']), 'ffmpeg-published-fix-required')
    ff_negative = ffmpeg_findings(ff, '8.1.1')
    require({'CVE-2026-8461', 'CVE-2026-30999'} <= set(ff_negative), 'ffmpeg-affected-control')
    index = fetch(OPENSSL)
    names = re.findall(r'<h2 id="(CVE-[0-9]{4}-[0-9]{4,})">\1</h2>', index.decode())
    require(1 <= len(names) <= 128 and len(names) == len(set(names))
            and 'CVE-2026-35189' in names
            and len(names) == len(re.findall(r'id="CVE-[0-9]{4}-[0-9]{4,}"', index.decode())), 'openssl-complete-branch-index')
    def read(name):
        raw = fetch('https://openssl-library.org/news/secjson/' + name.lower() + '.json', 262144)
        record = packages.parse(raw)
        current = openssl_findings(record, observation['srtp_openssl'], name)
        current += openssl_findings(record, observation['cryptography_openssl'], name)
        negative = openssl_findings(record, '3.5.4', name)
        return name, hashlib.sha256(raw).hexdigest(), current, negative
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(read, names))
    require(not any(row[2] for row in records), 'openssl-published-fix-required')
    negative = sorted({v for row in records for v in row[3]})
    require('CVE-2026-35189' in negative, 'openssl-affected-control')
    return dict(ffmpeg=dict(url=FFMPEG, sha256=hashlib.sha256(ff).hexdigest(), findings=[],
                           affected_control=dict(version='8.1.1', advisory_ids=ff_negative, executed=False)),
                openssl=dict(url=OPENSSL, sha256=hashlib.sha256(index).hexdigest(), findings=[],
                             records={row[0]: row[1] for row in records},
                             affected_control=dict(version='3.5.4', advisory_ids=negative, executed=False)),
                scope='FFmpeg selected-branch published fixes and OpenSSL 4.0 official CVE records',
                checked_on=datetime.now(timezone.utc).isoformat(), complete_native_advisory_coverage=False)


def extract(raw, directory, root):
    archive = directory / (root + '.tar.gz')
    archive.write_bytes(raw)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        require(0 < len(members) <= 40000 and sum(m.size for m in members) <= 256 * 1024 * 1024, 'archive-budget')
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            require(path.parts and path.parts[0] == root and not path.is_absolute() and '..' not in path.parts
                    and member.name not in seen and (member.isfile() or member.isdir() or member.issym()), 'archive-path')
            if member.issym():
                target = PurePosixPath(member.linkname)
                # The pinned sources need no escaping symlinks.
                require(not target.is_absolute() and '..' not in target.parts, 'archive-symlink')
            seen.add(member.name)
        source.extractall(directory, members=members, filter='data')
    return directory / root


def execute(command, work, name, *, cwd=None, environment=None, seconds=300):
    # Reuse the qualified bounded process-group runner: timeout/output overflow
    # retires compiler/make descendants before reaping the reserved group leader.
    spec = importlib.util.spec_from_file_location('native_build_process', ROOT / 'gateway/fuzz/run.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    status, _ = module.execute(command, work, name, environment or os.environ, seconds, cwd=cwd or work)
    require(status == 0, 'native-build-status-' + name)


def install_requirements(expected, hashes, built, wheels):
    lines = []
    for name in sorted(expected):
        # pip may prefer a same-version PyPI wheel over --find-links. A direct
        # owned wheel reference makes the replacement mandatory, still hashed.
        selected = name + '==' + expected[name]
        if name in built:
            selected = name + ' @ ' + (wheels / built[name]['filename']).as_uri()
        lines.append(selected + ' ' + ' '.join('--hash=sha256:' + h for h in hashes[name]) + '\n')
    return ''.join(lines)


def prepare(work, runtime=None):
    require(sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12)
            and sys.platform in ['linux', 'darwin'], 'candidate-runtime')
    require(not os.path.lexists(work), 'new-owned-work-directory')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, timeout=10, text=True).strip()
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, timeout=10).strip(), 'clean-candidate-source')
    runtime = runtime or work / 'runtime'
    require(runtime.is_absolute() and not os.path.lexists(runtime), 'new-runtime-directory')
    before = inputs()
    recipe = packages.parse(packages.bounded(ROOT / 'gateway/tests/native-python.json'))
    require(recipe['schema'] == 1, 'recipe-version')
    os.umask(0o077)
    work.mkdir(mode=0o700)
    env = {k: os.environ[k] for k in ['PATH', 'SDKROOT', 'DEVELOPER_DIR'] if k in os.environ}
    env.update(HOME=str(work), LANG='C', LC_ALL='C', PYTHONNOUSERSITE='1', PIP_CONFIG_FILE=os.devnull,
               PIP_NO_INPUT='1', PIP_DISABLE_PIP_VERSION_CHECK='1', SOURCE_DATE_EPOCH=str(recipe['source_date_epoch']))
    sources = {}
    for name, pin in [('openssl', recipe['openssl']), ('libsrtp', recipe['libsrtp']), *recipe['python_sources'].items()]:
        print('NATIVE_PYTHON_PHASE source-' + name, flush=True)
        raw = fetch(pin['url'], 64 * 1024 * 1024)
        require(hashlib.sha256(raw).hexdigest() == pin['sha256'], 'native-source-digest')
        sources[name] = extract(raw, work, pin['root'])
    build = work / 'builder'
    execute([sys.executable, '-I', '-m', 'venv', str(build)], work, 'builder-venv', environment=env)
    python = str(build / 'bin/python')
    _, _, _, hashes = packages.lock('media')
    builders = dict(recipe['build_packages'])
    for name in ['cffi', 'pycparser']:
        builders[name] = dict(version=packages.lock('media')[1][name], hashes=hashes[name])
    build_lock = work / 'build-lock.txt'
    build_lock.write_text(''.join(name + '==' + pin['version'] + ' ' + ' '.join('--hash=sha256:' + h for h in pin.get('hashes', [pin.get('sha256')])) + '\n' for name, pin in sorted(builders.items())))
    execute([python, '-m', 'pip', 'install', '--require-hashes', '--only-binary=:all:', '-r', str(build_lock)], work, 'builder-install', environment=env)
    crypto = work / 'crypto'; srtp = work / 'srtp'
    print('NATIVE_PYTHON_PHASE build-openssl', flush=True)
    execute(['perl', 'Configure', 'no-shared', 'no-module', 'no-tests', '-fPIC', '--prefix=' + str(crypto), '--libdir=lib'], work, 'openssl-config', cwd=sources['openssl'], environment=env)
    execute(['make', '-s', '-j4', 'build_libs'], work, 'openssl-build', cwd=sources['openssl'], environment=env)
    execute(['make', 'install_dev'], work, 'openssl-install', cwd=sources['openssl'], environment=env)
    native_env = dict(env, CFLAGS='-fPIC')
    print('NATIVE_PYTHON_PHASE build-and-test-srtp', flush=True)
    execute(['./configure', '--prefix=' + str(srtp), '--enable-openssl', '--with-openssl-dir=' + str(crypto), '--disable-shared'], work, 'srtp-config', cwd=sources['libsrtp'], environment=native_env)
    execute(['make', '-s', '-j4'], work, 'srtp-build', cwd=sources['libsrtp'], environment=native_env)
    execute(['make', 'runtest'], work, 'srtp-tests', cwd=sources['libsrtp'], environment=native_env)
    execute(['make', 'install'], work, 'srtp-install', cwd=sources['libsrtp'], environment=native_env)
    patch = recipe['aiortc_backport']; path = sources['aiortc'] / patch['path']; raw = path.read_text()
    require(raw.count(patch['before']) == 1, 'exact-upstream-backport-context')
    path.write_text(raw.replace(patch['before'], patch['after']))
    wheels = work / 'wheels'; wheels.mkdir()
    native_env = dict(env, CFLAGS='-I' + str(srtp / 'include') + ' -I' + str(crypto / 'include'),
                      LDFLAGS='-L' + str(srtp / 'lib') + ' -L' + str(crypto / 'lib'))
    for name in ['aiortc', 'pylibsrtp']:
        execute([python, '-m', 'pip', 'wheel', '--no-build-isolation', '--no-deps', '--wheel-dir', str(wheels), str(sources[name])], work, name + '-wheel', environment=native_env)
    built = {}
    for path in wheels.iterdir():
        name = path.name.split('-')[0]
        require(name in recipe['python_sources'] and name not in built and path.suffix == '.whl', 'built-wheel-set')
        built[name] = dict(filename=path.name, sha256=digest(path))
    require(set(built) == {'aiortc', 'pylibsrtp'}, 'complete-built-wheels')
    _, expected, _, hashes = packages.lock('media'); expected = dict(expected); hashes = dict(hashes)
    expected['av'] = recipe['av']['version']; hashes['av'] = recipe['av']['wheel_hashes']
    for name, wheel in built.items(): hashes[name] = [wheel['sha256']]
    install_lock = work / 'install-lock.txt'
    install_lock.write_text(install_requirements(expected, hashes, built, wheels))
    execute([sys.executable, '-I', '-m', 'venv', str(runtime)], work, 'runtime-venv', environment=env)
    actual = str(runtime / 'bin/python')
    print('NATIVE_PYTHON_PHASE install-candidate', flush=True)
    execute([actual, '-m', 'pip', 'install', '--no-cache-dir', '--require-hashes', '--only-binary=:all:', '--find-links', str(wheels), '-r', str(install_lock)], work, 'runtime-install', environment=env)
    execute([actual, '-m', 'pip', 'check'], work, 'runtime-check', environment=env)
    require(before == inputs() and subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, timeout=10, text=True).strip() == head
            and not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, timeout=10).strip(), 'native-build-source-change')
    receipt = dict(schema=1, checkout=dict(clean=True, commit=head), python=sys.version.split()[0], platform=sys.platform, inputs=before, packages=expected, source_archives={name: pin['sha256'] for name, pin in [('openssl', recipe['openssl']), ('libsrtp', recipe['libsrtp']), *recipe['python_sources'].items()]},
                   built_wheels=built, aiortc_backport=patch['commit'], srtp_self_tests=True,
                   libraries={name: digest(path) for name, path in [('openssl_crypto', crypto / 'lib/libcrypto.a'), ('srtp', srtp / 'lib/libsrtp2.a')]},
                   compiler_sha256=digest(Path(__import__('shutil').which('cc')).resolve()),
                   install_lock_sha256=digest(install_lock), native_advisory_closure=False,
                   physical_qualified=False, fips_validated=False, deployment_permitted=False)
    (runtime / 'native-build.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    return receipt


def observe():
    import av
    import pylibsrtp
    from cryptography.hazmat.backends import default_backend
    paths = list(Path(pylibsrtp.__file__).parent.glob('_binding*.so'))
    require(len(paths) == 1, 'single-srtp-native-image')
    library = ctypes.CDLL(str(paths[0]))
    srtp_version = library.srtp_get_version_string; srtp_version.restype = ctypes.c_char_p
    ssl_version = library.OpenSSL_version; ssl_version.argtypes = [ctypes.c_int]; ssl_version.restype = ctypes.c_char_p
    def ssl_number(text):
        match = re.fullmatch(r'OpenSSL ([0-9]+\.[0-9]+\.[0-9]+) [0-9A-Za-z ]+', text)
        require(match is not None, 'observed-openssl-version')
        return match[1]
    text = srtp_version().decode('ascii')
    require(re.fullmatch(r'libsrtp2 [0-9]+\.[0-9]+\.[0-9]+', text), 'observed-srtp-version')
    return dict(ffmpeg=av.ffmpeg_version_info, srtp=text.split()[1],
                srtp_openssl=ssl_number(ssl_version(0).decode('ascii')),
                cryptography_openssl=ssl_number(default_backend().openssl_version_text()))


def installed_files():
    # Verify native payloads against wheel RECORD digests, then hash actual bytes.
    import base64
    result = {}
    for name in ['av', 'pylibsrtp', 'cryptography']:
        distribution = metadata.distribution(name); selected = {}
        for item in distribution.files or []:
            if not (str(item).endswith('.so') or '.so.' in str(item) or str(item).endswith('.dylib')): continue
            path = distribution.locate_file(item)
            require(path.resolve().is_relative_to(Path(sys.prefix).resolve())
                    and not path.is_symlink() and path.is_file() and item.hash is not None and item.hash.mode == 'sha256', 'native-record-identity')
            actual = digest(path)
            require(base64.urlsafe_b64encode(bytes.fromhex(actual)).rstrip(b'=').decode() == item.hash.value, 'native-payload-integrity')
            selected[str(item)] = actual
        require(0 < len(selected) <= 160, 'native-payload-count')
        result[name] = selected
    return result


def qualify(report, audit_python):
    require(not os.path.lexists(report), 'new-public-report')
    receipt = packages.parse(packages.bounded(Path(sys.prefix) / 'native-build.json', 65536))
    require(receipt['schema'] == 1 and receipt['inputs'] == inputs() and receipt['srtp_self_tests'] is True
            and receipt['checkout']['clean'] is True
            and subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, timeout=10, text=True).strip() == receipt['checkout']['commit']
            and not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, timeout=10).strip(), 'current-build-receipt')
    actual = {}
    for distribution in metadata.distributions():
        name = packages.normal(distribution.metadata['Name'])
        require(name not in actual, 'duplicate-runtime-package')
        actual[name] = distribution.version
    for name in ['pip', 'setuptools', 'wheel']: actual.pop(name, None)
    require(actual == receipt['packages'], 'actual-candidate-package-graph')
    native = observe(); require(native == dict(ffmpeg='9.0.2', srtp='2.7.0', srtp_openssl='4.0.3', cryptography_openssl='4.0.3'), 'reviewed-native-identities')
    files = installed_files(); advisories = audit_native(native)
    # Audit the actual 26-package candidate graph independently of the old lock.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='nxrs-native-audit-') as temporary:
        directory = Path(temporary); requirements = directory / 'actual.txt'
        status, raw = packages.request(audit_python, ['--version'], directory, 30)
        require(status == 0 and raw.strip() == b'pip-audit 2.10.1', 'scanner-version')
        requirements.write_text(''.join(n + '==' + v + '\n' for n, v in sorted(actual.items())))
        status, raw = packages.request(audit_python, ['--disable-pip', '--no-deps', '--strict', '--format', 'json', '--progress-spinner', 'off', '--cache-dir', str(directory / 'fresh-cache'), '-r', str(requirements)], directory)
        packages.response(status, raw, actual)
    # Reuse the real owned HTTP/H264/DTLS/SRTP witness, with the candidate's
    # independent inventory instead of the original wheel-only admission lock.
    spec = importlib.util.spec_from_file_location('candidate_loopback', ROOT / 'gateway/tests/python_media_reference.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    def inventory(group):
        require(group == 'media', 'candidate-group')
        require(observe() == native and installed_files() == files, 'native-runtime-changed')
        return dict(inputs=inputs(), packages=actual, native_payload_sha256=files)
    import asyncio
    loopback = asyncio.run(asyncio.wait_for(module.exercise(inventory('media'), collect=inventory), 40))
    require(loopback['decoded_h264_frames'] == 6 and loopback['joined_cleanup'] is True, 'candidate-loopback')
    require(receipt['inputs'] == inputs(), 'qualification-source-change')
    value = dict(schema=1, scope='audited-host-python-native-media-environment', qualified=True, build=receipt,
                 observed=native, native_payload_sha256=files, advisories=advisories,
                 actual_python_package_advisories=dict(packages=len(actual), findings=[]),
                 native_http_parser=True, decoded_h264_frames=6, joined_cleanup=True,
                 gateway_composition_qualified=False, os_advisory_closure=False,
                 native_advisory_closure=False, physical_qualified=False, fips_validated=False, deployment_permitted=False)
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    require(len(raw) <= 65536, 'public-report-budget')
    admission = Path(sys.prefix) / 'native-qualification.json'
    require(not os.path.lexists(admission), 'new-native-admission')
    with open(report, 'xb') as output: output.write(raw)
    if report.absolute() != admission.absolute():
        with open(admission, 'xb') as output: output.write(raw)
    os.chmod(admission, 0o644)


def public_runtime(runtime):
    # Only installed public code and bounded receipts live here. Recording
    # fixtures switch UID; build sources/logs remain in the private work tree.
    import stat
    for directory, dirs, files in os.walk(runtime):
        os.chmod(directory, 0o755)
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode): continue
            require(stat.S_ISREG(mode), 'runtime-regular-file')
            os.chmod(path, 0o755 if mode & 0o111 else 0o644)


def validate_evidence(value):
    recipe = packages.parse(packages.bounded(ROOT / 'gateway/tests/native-python.json'))
    require(value['schema'] == 1 and value['scope'] == 'admitted-pinned-native-python-environment'
            and value['inputs'] == inputs() and value['qualified'] is True, 'native-evidence-identity')
    require(re.fullmatch('[0-9a-f]{64}', value['qualification_sha256'])
            and re.fullmatch('[0-9a-f]{40}', value['checkout'])
            and value['srtp_self_tests'] is True and type(value['decoded_h264_frames']) is int and value['decoded_h264_frames'] == 6
            and value['joined_cleanup'] is True and value['native_http_parser'] is True, 'native-evidence-execution')
    require(value['observed'] == dict(ffmpeg='9.0.2', srtp='2.7.0', srtp_openssl='4.0.3', cryptography_openssl='4.0.3'), 'native-evidence-versions')
    require(value['source_archives'] == {name: p['sha256'] for name, p in [('openssl',recipe['openssl']),('libsrtp',recipe['libsrtp']),*recipe['python_sources'].items()]}
            and value['aiortc_backport'] == recipe['aiortc_backport']['commit'], 'native-evidence-source')
    images = value['native_payload_sha256']
    require(set(images) == {'av','pylibsrtp','cryptography'} and len(images['pylibsrtp']) == len(images['cryptography']) == 1, 'native-image-set')
    for entries in images.values():
        require(isinstance(entries,dict) and 1 <= len(entries) <= 160 and all(isinstance(k,str)
                and len(k) <= 240 and not k.startswith('/') and re.fullmatch('[0-9a-f]{64}',v) for k,v in entries.items()), 'native-image-identities')
    require(value['actual_python_package_advisories'] == dict(packages=26,findings=[]), 'native-package-audit')
    audits=value['advisories']
    require(audits['scope'] == 'FFmpeg selected-branch published fixes and OpenSSL 4.0 official CVE records'
            and audits['complete_native_advisory_coverage'] is False, 'native-advisory-scope')
    for name,url,old,required in [('ffmpeg',FFMPEG,'8.1.1',{'CVE-2026-8461','CVE-2026-30999'}),('openssl',OPENSSL,'3.5.4',{'CVE-2026-35189'})]:
        check=audits[name];control=check['affected_control']
        require(check['url']==url and check['findings']==[] and re.fullmatch('[0-9a-f]{64}',check['sha256'])
                and control['version']==old and control['executed'] is False and required<=set(control['advisory_ids']), 'native-advisory-controls')
    records=audits['openssl']['records']
    require(isinstance(records,dict) and 1<=len(records)<=128 and 'CVE-2026-35189' in records
            and all(re.fullmatch('CVE-[0-9]{4}-[0-9]{4,}',k) and re.fullmatch('[0-9a-f]{64}',v) for k,v in records.items()), 'native-advisory-records')
    require(set(value['built_wheels'])=={'aiortc','pylibsrtp'} and all(re.fullmatch('[0-9a-f]{64}',v['sha256']) for v in value['built_wheels'].values()), 'native-built-wheel-identity')
    require(all(value[k] is False for k in ['native_advisory_closure','os_advisory_closure','physical_qualified','fips_validated','deployment_permitted']), 'native-evidence-scope')


def runtime_evidence():
    raw = packages.bounded(Path(sys.prefix) / 'native-qualification.json',65536)
    report = packages.parse(raw); build=report['build']
    receipt=packages.parse(packages.bounded(Path(sys.prefix) / 'native-build.json',65536))
    require(build==receipt and report['schema']==1 and report['scope']=='audited-host-python-native-media-environment'
            and report['qualified'] is True and build['checkout']['clean'] is True
            and report['gateway_composition_qualified'] is False, 'native-runtime-admission')
    require(all(report[k] is False for k in ['native_advisory_closure','os_advisory_closure','physical_qualified','fips_validated','deployment_permitted'])
            and all(build[k] is False for k in ['native_advisory_closure','physical_qualified','fips_validated','deployment_permitted']), 'native-runtime-scope')
    require(build['inputs']==inputs() and build['packages']==packages.lock('media')[1]
            and build['python']==sys.version.split()[0] and build['platform']==sys.platform, 'native-runtime-build-inputs')
    require(observe()==report['observed'] and installed_files()==report['native_payload_sha256'], 'native-runtime-payload-change')
    value=dict(schema=1,scope='admitted-pinned-native-python-environment',qualified=True,inputs=build['inputs'],
        qualification_sha256=hashlib.sha256(raw).hexdigest(),checkout=build['checkout']['commit'],
        source_archives=build['source_archives'],built_wheels=build['built_wheels'],aiortc_backport=build['aiortc_backport'],
        srtp_self_tests=build['srtp_self_tests'],observed=report['observed'],native_payload_sha256=report['native_payload_sha256'],
        actual_python_package_advisories=report['actual_python_package_advisories'],advisories=report['advisories'],
        native_http_parser=report['native_http_parser'],decoded_h264_frames=report['decoded_h264_frames'],joined_cleanup=report['joined_cleanup'],
        native_advisory_closure=False,os_advisory_closure=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    validate_evidence(value); return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'qualify'])
    parser.add_argument('--runtime', type=Path); parser.add_argument('--qualify', action='store_true');
    parser.add_argument('--work', type=Path); parser.add_argument('--report', type=Path); parser.add_argument('--audit-python', type=Path)
    args = parser.parse_args()
    try:
        if args.mode == 'prepare':
            require(args.work is not None and args.work.is_absolute(), 'absolute-owned-work')
            prepare(args.work, args.runtime)
            runtime = args.runtime or args.work / 'runtime'
            if args.qualify:
                environment = {k: os.environ[k] for k in ['PATH', 'SDKROOT', 'DEVELOPER_DIR'] if k in os.environ}
                environment.update(HOME=str(args.work), LANG='C', LC_ALL='C', PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
                auditor = args.work / 'auditor'
                execute([sys.executable, '-I', '-m', 'venv', str(auditor)], args.work, 'auditor-venv', environment=environment)
                execute([str(auditor / 'bin/python'), '-m', 'pip', 'install', 'pip-audit==2.10.1'], args.work, 'auditor-install', environment=environment)
                execute([str(runtime / 'bin/python'), str(Path(__file__).resolve()), 'qualify', '--audit-python', str(auditor / 'bin/python'), '--report', str(runtime / 'native-qualification.json')], args.work, 'runtime-qualification', cwd=ROOT, environment=environment)
            public_runtime(runtime)
        else:
            require(args.report is not None and args.audit_python is not None, 'qualification-arguments')
            qualify(args.report, args.audit_python.absolute())
    except Exception as error:
        # Only our fixed categories; native/build/feed text stays private.
        category = str(error) if isinstance(error, Refusal) and re.fullmatch('[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        parser.exit(1, 'Native Python environment refused (' + category + ').\n')
    print('PASS: shared native Python environment ' + args.mode)


if __name__ == '__main__': main()
