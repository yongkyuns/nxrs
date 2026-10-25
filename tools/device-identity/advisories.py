#!/usr/bin/env python3
"""Current upstream advisory review for the exact executed host device SDK.

Native source, commands and compiler output stay private. The public receipt
binds the existing SDK witness, archive, reviewed Rust policy, resolved native
features and the complete official advisory index. It is not target or physical
qualification, a general library approval, or complete product advisory closure.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/boot'))
import upstream_advisories as upstream

INPUTS = ('tools/device-identity/advisories.py', 'tools/boot/upstream_advisories.py',
          'tools/boot/target_plan.py', 'tools/boot/qualify.py', 'tools/platform_config.py',
          'security/device-tls-advisory-review.json',
          'tests/host/test_device_tls_advisories.py')
FORBIDDEN = {'MBEDTLS_RSA_C', 'MBEDTLS_CIPHER_MODE_CBC', 'MBEDTLS_CIPHER_MODE_ECB',
             'MBEDTLS_SSL_CLI_C', 'MBEDTLS_SSL_SESSION_TICKETS', 'MBEDTLS_SSL_TICKET_C',
             'MBEDTLS_SSL_CACHE_C', 'MBEDTLS_SSL_CONTEXT_SERIALIZATION',
             'MBEDTLS_SSL_RENEGOTIATION', 'MBEDTLS_SSL_PROTO_DTLS', 'MBEDTLS_SSL_EARLY_DATA',
             'MBEDTLS_CTR_DRBG_C', 'MBEDTLS_PSA_ASSUME_EXCLUSIVE_BUFFERS'}


def require(value):
    if not value:
        raise ValueError('device TLS advisory qualification refused')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, maximum=65536):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum)
    raw = path.read_bytes(); require(len(raw) <= maximum)
    return raw


def capture():
    deadline = time.monotonic() + 180
    parser = upstream.AdvisoryLinks()
    parser.feed(upstream.fetch(upstream.MBED, deadline, 1024 * 1024).decode('utf-8'))
    parser.text()
    require(0 < len(parser.links) <= 256)
    def article(slug):
        return upstream.mbed_projection(slug, upstream.fetch(upstream.MBED + slug + '/', deadline, 256 * 1024))
    with ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(article, sorted(parser.links)))
    return dict(checked_at=datetime.now(timezone.utc).isoformat(), records=records)


def features(raw):
    require(len(raw) <= 1024 * 1024)
    values = {}
    for name, value in re.findall(r'^#define ([A-Z][A-Z0-9_]*)(?:[ \t]+([^\n]*))?$', raw.decode('utf-8'), re.M):
        if name.startswith('PSA_WANT_') or name in FORBIDDEN or re.fullmatch(r'MBEDTLS_(?:[A-Z0-9_]+_C|SSL_[A-Z0-9_]+|KEY_EXCHANGE_[A-Z0-9_]+_ENABLED)', name):
            require(name not in values)
            # These are configuration switches, not header string/path macros.
            require(value in ('', '0', '1', '4096') or re.fullmatch(r'PSA_WANT_[A-Z0-9_]{1,80}', value))
            if value != '0':
                values[name] = value
    require(0 < len(values) <= 512 and not FORBIDDEN.intersection(values))
    require(values.get('PSA_WANT_ALG_GCM') == '1' and 'MBEDTLS_SSL_SRV_C' in values)
    return values


def review(captured, catalogue, sdk, current_inputs, resolved_features, now=None):
    now = now or datetime.now(timezone.utc)
    require(catalogue['schema'] == 1 and catalogue['scope'] == 'exact-host-device-tls-sdk')
    checked = datetime.fromisoformat(captured['checked_at'])
    reviewed = datetime.fromisoformat(catalogue['reviewed_at'])
    expires = datetime.fromisoformat(catalogue['valid_until'])
    require(all(t.tzinfo is not None for t in (now, checked, reviewed, expires)))
    require(0 <= (now - checked).total_seconds() <= 3600 and reviewed <= now < expires
            and 0 < (expires - reviewed).total_seconds() <= 31 * 86400)
    require(sdk == catalogue['sdk'])
    require(catalogue['policy_inputs'] and all(current_inputs.get(k) == v for k, v in catalogue['policy_inputs'].items()))
    require(resolved_features == catalogue['resolved_features'])
    # Recheck explicit dangerous switches as well as exact feature equality.
    require(not FORBIDDEN.intersection(resolved_features))
    records = captured['records']; expected = catalogue['advisories']
    require(isinstance(records, list) and 0 < len(records) <= 256 and len(expected) == len(records))
    actual = {r['id']: r for r in records}; known = {r['id']: r for r in expected}
    require(len(actual) == len(records) and len(known) == len(expected) and set(actual) == set(known))
    outcomes = []
    for identity in sorted(actual):
        record = actual[identity]; entry = known[identity]
        require(record == {k: entry[k] for k in ('id', 'url', 'sha256')})
        require(entry['assessment'] in {'fixed-in-selected-source', 'excluded-by-resolved-profile', 'explicit-certificate-profile', 'not-selected-library'})
        require(type(entry['reason']) is str and 30 <= len(entry['reason']) <= 640)
        outcomes.append(dict(id=identity, assessment=entry['assessment']))
    return dict(checked_at=captured['checked_at'], advisory_count=len(outcomes),
                reviewed_advisories=outcomes, official_records_sha256=digest(canonical(records)),
                review_sha256=digest(canonical(catalogue)))


def command_policy(unit, compiler, source, build):
    source = source.resolve(); build = build.resolve()
    location = Path(unit['directory']).resolve()
    unit_source = Path(unit['file']).resolve()
    require(location.is_relative_to(build) and unit_source.is_relative_to(source))
    arguments = shlex.split(unit['command']); require(arguments)
    require(Path(arguments[0]).resolve() == compiler.resolve())
    expected = {'-DNDEBUG', '-DTF_PSA_CRYPTO_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/crypto_config.h') + '"'}
    if not unit_source.is_relative_to(source / 'tf-psa-crypto'):
        expected.add('-DMBEDTLS_CONFIG_FILE="' + str(ROOT / 'tools/device-identity/mbedtls_config.h') + '"')
    definitions = []; outputs = 0; sources = 0; index = 1
    while index < len(arguments):
        arg = arguments[index]; index += 1
        if arg.startswith('-D'):
            definitions.append(arg)
        elif arg.startswith('-I'):
            include = Path(arg[2:]).resolve()
            require(include.is_relative_to(source) or include in {build / 'library', build / 'tf-psa-crypto/core',
                                                                build / 'tf-psa-crypto/include', ROOT / 'driver/device-identity/src/ffi'})
        elif arg in ('-o', '-c', '-arch', '-isysroot'):
            require(index < len(arguments)); value = arguments[index]; index += 1
            if arg == '-o':
                outputs += 1; require((location / value).resolve().is_relative_to(build))
            elif arg == '-c':
                sources += 1; require(Path(value).resolve() == unit_source)
            elif arg == '-arch': require(value in ('arm64', 'x86_64'))
            else: require(Path(value).is_dir())
        else:
            require(arg in {'-O0','-O1','-O2','-O3','-Os','-Oz','-fno-lto','-fno-omit-frame-pointer',
                            '-fsanitize=address,undefined','-fno-sanitize-recover=all','-fPIC','-std=c99'}
                    or (re.fullmatch(r'-W[A-Za-z0-9_=,-]+', arg) and not arg.startswith('-Wp,')))
    require(outputs == sources == 1 and len(definitions) == len(expected) and set(definitions) == expected)


def inspect(work, catalogue):
    work = work.resolve(strict=True)
    raw = read(work / 'qualification.json'); sdk = json.loads(raw)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, timeout=10).decode().strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, timeout=10).strip())
    require(sdk['repository_head'] == head and sdk['repository_has_changes'] is dirty)
    require(sdk['scope'] == 'actual-volatile-device-identity-host-sdk-and-issuer' and sdk['qualified'] is True)
    require(sdk['native_advisory_closure'] is False and sdk['physical_qualified'] is False and sdk['deployment_permitted'] is False)
    # Import the named identity qualifier without colliding with boot.qualify.
    import importlib.util
    spec = importlib.util.spec_from_file_location('advisory_identity', ROOT / 'tools/device-identity/qualify.py')
    identity = importlib.util.module_from_spec(spec); spec.loader.exec_module(identity)
    expected = {name: digest(read(ROOT / name, 4 * 1024 * 1024)) for name in identity.INPUTS}
    require(sdk['inputs'] == expected)
    pins = json.loads(read(ROOT / 'security/device-identity-dependencies.json'))['mbedtls']
    require(sdk['sdk'] == pins == catalogue['sdk'])
    require(digest(read(work / 'sdk.tar.bz2', 16 * 1024 * 1024)) == pins['sha256'])
    source = work / ('mbedtls-' + pins['version'])
    for path, macro, version in [('include/mbedtls/build_info.h', 'MBEDTLS_VERSION_STRING', pins['version']),
        ('tf-psa-crypto/include/tf-psa-crypto/build_info.h', 'TF_PSA_CRYPTO_VERSION_STRING', pins['bundled_tf_psa_crypto_version'])]:
        require(re.findall(r'^#define\s+' + macro + r'\s+"([0-9.]+)"\s*$', read(source / path).decode(), re.M) == [version])
    variants = {}; resolved = None
    for name, sanitized in [('normal', False), ('sanitized', True)]:
        variant = sdk['variants'][name]; folder = work / name; build = folder / 'build'
        require(variant['qualified'] is True and variant['native_c_asan_ubsan'] is sanitized
                and variant['required_native_tests'] == list(identity.TESTS)
                and variant['compiled_unsafe_config_refusals'] == 4 and variant['compiled_unsafe_tls_config_refusals'] == 17)
        artifacts = {'libmbedtls.a': build / 'library/libmbedtls.a', 'libmbedx509.a': build / 'library/libmbedx509.a',
                     'libtfpsacrypto.a': build / 'tf-psa-crypto/core/libtfpsacrypto.a',
                     'libnxrs_device_identity_sdk.a': folder / 'libnxrs_device_identity_sdk.a', 'sdk-tests': folder / 'sdk-tests'}
        require(set(variant['artifacts']) == set(artifacts))
        for label, path in artifacts.items():
            require(digest(read(path, 64 * 1024 * 1024)) == variant['artifacts'][label])
        cache = read(build / 'CMakeCache.txt', 1024 * 1024).decode()
        compilers = re.findall(r'^CMAKE_C_COMPILER:(?:FILEPATH|STRING)=(.+)$', cache, re.M)
        require(len(compilers) == 1 and digest(read(Path(compilers[0]).resolve(), 256 * 1024 * 1024)) == variant['compiler']['sdk_driver_sha256'])
        commands_raw = read(build / 'compile_commands.json', 4 * 1024 * 1024)
        commands = json.loads(commands_raw); require(len(commands) == variant['sdk_compilation_units'] and 1 <= len(commands) <= 256)
        for unit in commands:
            command_policy(unit, Path(compilers[0]), source, build)
        # Replay a checked Mbed TLS unit's real flags, including its SDK root
        # and architecture. Replace only source/output with a macro-only probe.
        probe = next(unit for unit in commands if not Path(unit['file']).resolve().is_relative_to((source / 'tf-psa-crypto').resolve()))
        arguments = shlex.split(probe['command']); command = [arguments[0]]; index = 1
        while index < len(arguments):
            if arguments[index] in ('-c', '-o'): index += 2
            else: command.append(arguments[index]); index += 1
        command.extend(['-dM', '-E', '-x', 'c', '-'])
        result = subprocess.run(command, cwd=probe['directory'], input=b'#include <mbedtls/build_info.h>\n',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        require(result.returncode == 0); selected = features(result.stdout)
        if resolved is None: resolved = selected
        else: require(resolved == selected)
        variants[name] = dict(artifacts=variant['artifacts'], compiler_sha256=variant['compiler']['sdk_driver_sha256'],
                              compilation_units=len(commands), commands_sha256=digest(commands_raw))
    expected.update({name: digest(read(ROOT / name, 4 * 1024 * 1024)) for name in INPUTS})
    return sdk, expected, resolved, dict(sdk_qualification_sha256=digest(raw), variants=variants)


def run(work):
    catalogue = json.loads(read(ROOT / 'security/device-tls-advisory-review.json', 128 * 1024))
    sdk, inputs, resolved, proof = inspect(work, catalogue)
    assessment = review(capture(), catalogue, sdk['sdk'], inputs, resolved)
    require(inputs == {name: digest(read(ROOT / name, 4 * 1024 * 1024)) for name in inputs})
    return dict(schema=1, scope='exact-executed-host-device-tls-sdk-advisories', qualified=True,
                repository_head=sdk['repository_head'], repository_has_changes=sdk['repository_has_changes'],
                inputs=inputs, sdk=sdk['sdk'], resolved_features=resolved, executed_sdk=proof, **assessment,
                native_advisory_closure=False, target_qualified=False, physical_qualified=False,
                fips_validated=False, deployment_permitted=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-work', type=Path, required=True); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.sdk_work)
        raw = canonical(report) + b'\n'; require(len(raw) <= 65536)
        with args.out.open('xb') as stream: stream.write(raw)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(upstream.public_failure(error), file=sys.stderr)
        raise SystemExit(1) from None
    print('Exact host device TLS SDK advisory review passed; target and complete product coverage remain open')


if __name__ == '__main__':
    main()
