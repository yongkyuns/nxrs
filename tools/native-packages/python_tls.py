#!/usr/bin/env python3
"""Qualify one upstream TLS backport in an owned disposable build, never install it."""
import argparse
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
RECIPE = 'security/python-tls-candidate.json'
PATCH = 'tools/native-packages/patches/python312-tls-distrust.patch'
INPUTS = (RECIPE, PATCH, 'tools/native-packages/patches/CPython-LICENSE.txt', 'tools/native-packages/python_tls.py',
          'tests/host/test_python_tls_candidate.py', '.github/workflows/python-tls-candidate.yml')
SCOPE = 'hosted-private-python-tls-backport-experiment'
FLAGS = ('gateway_integrated', 'distribution_runtime_replaced',
         'descriptor_signature_verified', 'source_to_installed_binary_join_qualified', 'advisory_applicability_qualified',
         'full_native_advisory_closure', 'bootstrap_environment_qualified',
         'signature_key_revocation_lifecycle_qualified', 'physical_qualified',
         'fips_validated', 'deployment_permitted')
MAX_TAR = 128 * 1024 * 1024


class Refusal(ValueError): pass


def require(value, category):
    if not value: raise Refusal(category)


def digest(raw): return hashlib.sha256(raw).hexdigest()


def parse(raw):
    def unique(rows):
        value = {}
        for key, item in rows:
            require(key not in value, 'duplicate-json-key'); value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def recipe():
    value = parse((ROOT / RECIPE).read_bytes())
    require(value['schema'] == 1 and value['scope'] == SCOPE
            and value['case'] == 'python312-tls-distrust'
            and value['source'] == 'python3.12' and value['source_version'] == '3.12.3-1ubuntu0.17'
            and value['source_archive_root'] == 'Python-3.12.3' and value['series_entries'] == 82,
            'exact-python-source-case')
    require(value['configure_regeneration'] == dict(tool='/usr/bin/autoconf',version='2.71'),
            'distribution-configure-regeneration')
    upstream = value['upstream']; fixture = value['fixture']
    require(upstream['merge'] == 'a17d9ef549008133eadf20689b1f4787902efe5b'
            and upstream['url'] == 'https://github.com/python/cpython/pull/160571'
            and upstream['module_patch_path'] == PATCH
            and upstream['header_git_blob'] == 'bbdb56dcfe671ed0a616d8c5ff2cc172854cfdbb',
            'exact-upstream-backport')
    require(digest((ROOT / PATCH).read_bytes()) == upstream['module_patch_sha256'], 'backport-patch-digest')
    require(fixture['url'] == 'https://raw.githubusercontent.com/python/cpython/' + upstream['merge']
            + '/Lib/test/certdata/distrusted.zip' and fixture['size'] == 5062
            and fixture['git_blob'] == '83184329a5ab9e076e66513bfaff4e3bbde53762'
            and fixture['sha256'] == '8a9874e20b8583e80e9d7d6c82ea566c22d50458a93bfef6400d70dde132a053',
            'exact-public-upstream-fixture')
    return value


def unpack(path, identity, directory):
    require(path.stat().st_size == identity['size'] and 0 < identity['size'] <= 64 * 1024 * 1024,
            'source-file-budget')
    with path.open('rb') as source: raw = source.read(identity['size'] + 1)
    require(len(raw) == identity['size'] and digest(raw) == identity['sha256'], 'authenticated-source-bytes')
    with lzma.open(path, 'rb') as source: data = source.read(MAX_TAR + 1)
    require(len(data) <= MAX_TAR, 'source-decompression-budget')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        rows = archive.getmembers(); seen = set()
        require(0 < len(rows) <= 20000 and sum(r.size for r in rows) <= MAX_TAR, 'source-tar-budget')
        for row in rows:
            name = PurePosixPath(row.name)
            require(name.parts and name.as_posix() == row.name.rstrip('/')
                    and not name.is_absolute() and '..' not in name.parts
                    and row.name not in seen and (row.isfile() or row.isdir()), 'source-regular-path')
            seen.add(row.name)
            row.mode = 0o700 if row.isdir() else 0o600 | (row.mode & 0o100)
        directory.mkdir(mode=0o700)
        archive.extractall(directory, members=rows, filter='data')


def series(source):
    folder = source / 'debian/patches'; names = []
    for line in (folder / 'series').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        fields = line.split(); require(len(fields) == 1, 'quilt-series-options')
        name = PurePosixPath(fields[0])
        require(not name.is_absolute() and '..' not in name.parts
                and fields[0] not in names, 'unique-quilt-series-path')
        patch = folder / fields[0]
        require(patch.is_file() and not patch.is_symlink() and patch.stat().st_size <= 4 * 1024 * 1024,
                'quilt-patch-file')
        text = patch.read_text()
        for value in re.findall(r'^(?:---|\+\+\+) (\S+)', text, re.M):
            if value == '/dev/null': continue
            path = PurePosixPath(value)
            require(not path.is_absolute() and '..' not in path.parts and len(path.parts) >= 2,
                    'quilt-target-path')
        names.append(fields[0])
    require(len(names) == 82, 'complete-distribution-series')
    return [folder / n for n in names]


def fixture(raw, expected, directory):
    require(len(raw) == expected['size'] and digest(raw) == expected['sha256'], 'upstream-fixture-digest')
    require(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
            == expected['git_blob'], 'upstream-fixture-git-blob')
    wanted = {'distrusted_keycert.pem': 5493, 'distrusted_intermediate.pem': 1241, 'distrusted_root.pem': 810}
    payloads = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        rows = archive.infolist(); require(len(rows) == 4, 'fixture-file-count')
        for row in rows:
            if row.filename == 'distrusted/':
                require(row.is_dir() and row.file_size == 0, 'fixture-directory'); continue
            name = PurePosixPath(row.filename)
            require(len(name.parts) == 2 and name.parts[0] == 'distrusted'
                    and name.name in wanted and name.name not in payloads
                    and not row.is_dir() and row.flag_bits & 1
                    and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG)
                    and row.file_size == wanted[name.name], 'fixture-regular-file')
            # This public password is part of CPython's regression fixture, not a secret.
            body = archive.read(row, pwd=b'Brave Sir Robin')
            require(len(body) == row.file_size, 'fixture-file-size'); payloads[name.name] = body
    require(set(payloads) == set(wanted), 'complete-upstream-fixture')
    directory.mkdir(mode=0o700)
    for name, body in payloads.items():
        with os.fdopen(os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
            out.write(body)


def handshake(client, server):
    import ssl
    ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
    left = client.wrap_bio(ci, co, server_side=False, server_hostname='localhost')
    right = server.wrap_bio(si, so, server_side=True); done = [False, False]
    for _ in range(128):
        for i, peer in enumerate((left, right)):
            if done[i]: continue
            try: peer.do_handshake(); done[i] = True
            except ssl.SSLWantReadError: pass
        for outgoing, incoming in ((co, si), (so, ci)):
            require(outgoing.pending <= 65536, 'tls-bio-budget')
            if outgoing.pending: incoming.write(outgoing.read())
        if all(done):
            left.write(b'nxrs-owned-tls-witness'); si.write(co.read())
            require(right.read(64) == b'nxrs-owned-tls-witness', 'tls-application-progress')
            return left.version()
    raise Refusal('tls-handshake-progress-budget')


def validate_witness(value, phase):
    require(type(value) is dict and set(value) == {'phase','ordinary_chain_progress','certificate_cases',
            'openssl_version','python_version','python_sha256','module_sha256','libraries'}, 'witness-schema')
    require(value['phase'] == phase and type(value['ordinary_chain_progress']) is int
            and value['ordinary_chain_progress'] == 2 and value['python_version'] == '3.12.3', 'witness-control-count')
    expected = [dict(tls=tls,mode=mode,outcome='accepted-affected-baseline' if phase=='baseline' else 'certificate-rejected-28')
                for tls in ('TLSv1.2','TLSv1.3') for mode in ('required','optional')]
    require(value['certificate_cases'] == expected, 'complete-baseline-candidate-matrix')
    require(type(value['openssl_version']) is str and re.fullmatch(r'OpenSSL [0-9]+\.[0-9]+\.[0-9]+ [0-9]{1,2} [A-Za-z]{3} [0-9]{4}',value['openssl_version']),
            'openssl-version-schema')
    require(type(value['libraries']) is dict and set(value['libraries']) == {'libssl.so.3','libcrypto.so.3'}, 'witness-library-pair')
    for h in [value['python_sha256'],value['module_sha256'],*value['libraries'].values()]:
        require(type(h) is str and re.fullmatch('[a-f0-9]{64}',h), 'witness-image-digest')
    return value


def build_failure(phase, status, raw, source_paths=(), source_identifiers=()):
    """Publish fixed tokens and line numbers, never copied compiler/feed text."""
    require(type(phase) is str and re.fullmatch(r'(?:quilt-(?:[0-9]|[1-7][0-9]|8[01])|distribution-autoconf|configure|baseline-build|upstream-backport|patched-build|baseline-witness|patched-witness)',phase)
            and type(status) is int and -127 <= status <= 255, 'build-failure-schema')
    markers = {
        'missing-build-target': b'No rule to make target',
        'missing-build-file': b'No such file or directory',
        'undefined-reference': b'undefined reference',
        'compiler-error': b'error:',
        'autoconf-regeneration': b'autoreconf',
        'missing-autoconf': b'autoreconf: not found',
        'missing-command': b'command not found',
        'permission-refusal': b'Permission denied',
        'python-import-refusal': b'ImportError',
        'python-missing-module': b'ModuleNotFoundError',
        'python-attribute-refusal': b'AttributeError',
        'python-syntax-refusal': b'SyntaxError',
        'python-runtime-refusal': b'Fatal Python error',
        'process-segmentation-fault': b'Segmentation fault',
        'process-aborted': b'Aborted',
    }
    lines = sorted({int(x) for x in re.findall(rb'\bMakefile:([0-9]{1,6})\b',raw)})[:8]
    # The caller captures this allowlist from authenticated public .c/.h files
    # before executing configure or make. Absolute/private paths never match.
    locations = []
    for path,line,column in re.findall(rb'(?:^|\n)([A-Za-z0-9_./-]{1,200}):([0-9]{1,6}):([0-9]{1,6}):\s*(?:fatal )?error:',raw):
        name = path.decode('ascii').removeprefix('./')
        if name in source_paths:
            value = dict(file=name,line=int(line),column=int(column))
            if value not in locations: locations.append(value)
    quoted = re.findall(rb'(?:\xe2\x80\x98|\x27|\x60)([A-Za-z_][A-Za-z0-9_]{0,79})(?:\xe2\x80\x99|\x27)',raw)
    identifiers = sorted({x.decode('ascii') for x in quoted if x.decode('ascii') in source_identifiers})[:16]
    return dict(phase=phase,exit_status=status,categories=[k for k,v in markers.items() if v in raw],
                generated_makefile_line_mentions=lines,public_source_locations=locations[:8],
                quoted_public_source_identifiers=identifiers,raw_output_exported=False)


def witness(source, certs, phase, report):
    import ssl
    import _ssl
    require(phase in ('baseline', 'patched'), 'named-tls-witness')
    normal = source / 'Lib/test/certdata'
    versions = [(ssl.TLSVersion.TLSv1_2, 'TLSv1.2'), (ssl.TLSVersion.TLSv1_3, 'TLSv1.3')]
    cases = []
    for version, label in versions:
        def contexts(distrusted, optional=False):
            root = certs / 'distrusted_root.pem' if distrusted else normal / 'capath/ceff1710.0'
            key = certs / 'distrusted_keycert.pem' if distrusted else normal / 'keycert3.pem'
            client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.load_verify_locations(cafile=str(root))
            client.verify_mode = ssl.CERT_OPTIONAL if optional else ssl.CERT_REQUIRED
            client.minimum_version = client.maximum_version = version
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); server.load_cert_chain(str(key))
            server.minimum_version = server.maximum_version = version
            return client, server
        require(handshake(*contexts(False)) == label, 'ordinary-chain-progress')
        for optional in (False, True):
            client, server = contexts(True, optional)
            try:
                negotiated = handshake(client, server)
            except ssl.SSLCertVerificationError as error:
                require(phase == 'patched' and error.verify_code == 28, 'exact-distrust-refusal')
                outcome = 'certificate-rejected-28'
            else:
                require(phase == 'baseline' and negotiated == label, 'affected-baseline-control')
                outcome = 'accepted-affected-baseline'
            cases.append(dict(tls=label,mode='optional' if optional else 'required',outcome=outcome))
    mapped = set()
    for line in Path('/proc/self/maps').read_text().splitlines():
        parts = line.split()
        if len(parts) == 6 and parts[-1].startswith('/'):
            path = Path(parts[-1]).resolve()
            if path.name.startswith(('libssl.so.', 'libcrypto.so.')): mapped.add(path)
    require(len(mapped) == 2, 'exact-openssl-library-pair')
    value = dict(phase=phase,ordinary_chain_progress=2,certificate_cases=cases,
                 openssl_version=ssl.OPENSSL_VERSION,python_version=sys.version.split()[0],
                 python_sha256=digest(Path(sys.executable).read_bytes()),module_sha256=digest(Path(_ssl.__file__).read_bytes()),
                 libraries={p.name:digest(p.read_bytes()) for p in sorted(mapped)})
    raw = (json.dumps(value,sort_keys=True) + '\n').encode(); require(len(raw) <= 8192, 'witness-budget')
    with report.open('xb') as out: out.write(raw)


def run(work, report):
    import source_payloads as s
    s.u.hosted()  # Before owned writes, network, package queries or native tools.
    require(work.is_absolute() and report.is_absolute() and work not in report.parents
            and not os.path.lexists(work) and not os.path.lexists(report), 'fresh-owned-work-and-report')
    head = s.u.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not s.u.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(), 'clean-candidate-checkout')
    before = {**s.inputs(), **{name:s.u.file_digest(ROOT / name) for name in INPUTS}}
    selected = recipe(); work.mkdir(mode=0o700); identity = work.stat()
    value = dict(schema=1,scope=SCOPE,qualified=False,verification_status='pending',checkout=dict(clean=True,commit=head),
                 inputs=before,recipe_sha256=s.u.file_digest(ROOT / RECIPE),receipt_is_unsigned=True,
                 source_payloads_authenticated=False,candidate_tls_callback_qualified=False,**dict.fromkeys(FLAGS,False))
    def save(token=None):
        require(len(json.dumps(value).encode()) <= 65536, 'public-receipt-budget')
        return s.u.save_report(report,value,token)
    token = save()
    spec = importlib.util.spec_from_file_location('python_tls_process',ROOT / 'gateway/fuzz/run.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    source_paths = set(); source_identifiers = set()
    def execute(command, name, cwd, seconds=60):
        status, raw = runner.execute(command,work,name,{'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'},seconds,cwd=cwd)
        if status != 0:value['build_failure'] = build_failure(name,status,raw,source_paths,source_identifiers)
        require(status == 0, 'candidate-build-status-' + name)
    try:
        source = s.run(selected['case'],work / 'archives',work / 'source.json')
        require(source['qualified'] is True and source['source_payloads_authenticated'] is True, 'signed-source-admission')
        source_files = source['source_record']['files']; value.update(source_payloads_authenticated=True,
            source_authentication_report_sha256=s.u.file_digest(work / 'source.json'),source_files=source_files)
        unpack(work / 'archives/python3.12_3.12.3.orig.tar.xz',source_files['python3.12_3.12.3.orig.tar.xz'],work / 'original')
        tree = work / 'original' / selected['source_archive_root']
        unpack(work / 'archives/python3.12_3.12.3-1ubuntu0.17.debian.tar.xz',source_files['python3.12_3.12.3-1ubuntu0.17.debian.tar.xz'],work / 'distribution')
        patches = series(work / 'distribution')
        for i, patch in enumerate(patches): execute(['/usr/bin/patch','-p1','--fuzz=0','--batch','--forward','-i',str(patch)],'quilt-'+str(i),tree)
        total_source = 0
        for folder in ('Modules','Python','Parser','Objects','Programs','Include'):
            for path in (tree / folder).rglob('*'):
                if not path.is_file() or path.suffix not in ('.c','.h'): continue
                raw = path.read_bytes(); total_source += len(raw)
                require(total_source <= 64 * 1024 * 1024, 'public-source-diagnostic-budget')
                source_paths.add(path.relative_to(tree).as_posix())
                source_identifiers.update(x.decode('ascii') for x in re.findall(rb'\b[A-Za-z_][A-Za-z0-9_]{0,79}\b',raw))
        ssl_source = tree / 'Modules/_ssl.c'; packaged = ssl_source.read_bytes()
        require(b'_ssl_verify_callback' not in packaged and b'distrusted_spki.h' not in packaged
                and b'SSL_CTX_set_verify(self->ctx, mode, verify_cb);' in packaged, 'affected-source-control')
        with urllib.request.urlopen(selected['fixture']['url'],timeout=30) as response: raw = response.read(65537)
        fixture(raw,selected['fixture'],work / 'certificates')
        # The authenticated debian/rules stamp-patch target regenerates this
        # file after quilt. Its configure.ac changes must reach the actual build.
        autoconf = s.u.command([selected['configure_regeneration']['tool'],'--version']).splitlines()[0]
        require(autoconf == 'autoconf (GNU Autoconf) ' + selected['configure_regeneration']['version'],
                'exact-distribution-autoconf-version')
        execute([selected['configure_regeneration']['tool']],'distribution-autoconf',tree,90)
        value['configuration'] = dict(regenerated_after_distribution_patches=True,
            autoconf_version=selected['configure_regeneration']['version'],
            configure_sha256=s.u.file_digest(tree / 'configure'),
            configure_ac_sha256=s.u.file_digest(tree / 'configure.ac'))
        execute(['./configure','--without-ensurepip','--disable-shared'],'configure',tree,180)
        execute(['/usr/bin/make','-s','-j4'],'baseline-build',tree,600)
        executable = tree / 'python'
        for phase in ('baseline','patched'):
            if phase == 'patched':
                execute(['/usr/bin/patch','-p1','--fuzz=0','--batch','--forward','-i',str(ROOT / PATCH)],'upstream-backport',tree)
                header = (tree / 'Modules/_ssl/distrusted_spki.h').read_bytes()
                require(hashlib.sha1(b'blob ' + str(len(header)).encode() + b'\0' + header).hexdigest()
                        == selected['upstream']['header_git_blob'], 'exact-upstream-spki-table')
                execute(['/usr/bin/make','-s','-j4'],'patched-build',tree,300)
            execute([str(executable),'-I',str(Path(__file__).resolve()),'witness','--source',str(tree),
                     '--certificates',str(work / 'certificates'),'--phase',phase,'--report',str(work / (phase+'.json'))],phase+'-witness',tree)
        baseline = validate_witness(parse((work / 'baseline.json').read_bytes()),'baseline')
        patched = validate_witness(parse((work / 'patched.json').read_bytes()),'patched')
        require(baseline['module_sha256'] != patched['module_sha256']
                and baseline['python_sha256'] == patched['python_sha256']
                and baseline['libraries'] == patched['libraries'], 'isolated-module-change')
        after = {**s.inputs(), **{name:s.u.file_digest(ROOT / name) for name in INPUTS}}
        require(before == after and head == s.u.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
                and not s.u.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(), 'unchanged-candidate-inputs')
        value.update(qualified=True,verification_status='complete',candidate_tls_callback_qualified=True,
                     source_version=selected['source_version'],quilt_series_entries=len(patches),
                     packaged_ssl_source_sha256=digest(packaged),candidate_ssl_source_sha256=digest(ssl_source.read_bytes()),
                     upstream=selected['upstream'],fixture_sha256=selected['fixture']['sha256'],baseline=baseline,patched=patched)
    except Exception as error:
        category = str(error) if isinstance(error,Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        value.update(qualified=False,verification_status='failed',failure_category=category)
        raise
    finally:
        current = work.lstat()
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino), 'owned-build-cleanup')
        shutil.rmtree(work); value['private_build_and_fixture_cleanup_joined'] = True; save(token)
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    prepare=sub.add_parser('qualify');prepare.add_argument('--work',type=Path,required=True);prepare.add_argument('--report',type=Path,required=True)
    probe=sub.add_parser('witness');probe.add_argument('--source',type=Path,required=True);probe.add_argument('--certificates',type=Path,required=True)
    probe.add_argument('--phase',choices=['baseline','patched'],required=True);probe.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.mode=='qualify':run(args.work,args.report)
        else:witness(args.source,args.certificates,args.phase,args.report)
    except Exception as error:
        category=str(error) if isinstance(error,Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Python TLS candidate refused ('+category+').\n')
    print('PASS: owned Python TLS callback experiment; integration and release gates remain open')


if __name__=='__main__': main()
