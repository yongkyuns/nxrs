#!/usr/bin/env python3
"""Authenticate exact reviewed Ubuntu source archives without extracting them."""
import argparse
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import payloads as p

u = p.u
ROOT = p.ROOT
SCOPE = 'authenticated-reviewed-ubuntu-source-payloads'
MAX_INDEX = 128 * 1024 * 1024
MAX_REPORT = 65536
INPUTS = ('tools/native-packages/source_payloads.py',
          'tests/host/test_native_source_payloads.py',
          'tools/native-packages/source-payloads.md')
# These are the exact bytes previously inspected for #123, not an authority.
# Only the signed archive index can authenticate them. No newer version or
# different bytes may silently replace that review's source material.
CASES = {'systemd-journald-wall': dict(
    source='systemd', version='255.4-1ubuntu8.18', component='main',
    directory='pool/main/s/systemd', descriptor='systemd_255.4-1ubuntu8.18.dsc',
    archives={
        'systemd_255.4.orig.tar.gz': dict(size=14952427,
            sha256='96e75bd08c57ad401677456fb88ef54a9f05bb1695693013bc6ecce839640fd5'),
        'systemd_255.4-1ubuntu8.18.debian.tar.xz': dict(size=273272,
            sha256='ec3ce620c3e072d34bbeaf9a4155cd91b5bee94eb412071bf81182f230ae2657'),
    })}
FLAGS = ('descriptor_signature_verified', 'source_to_installed_binary_join_qualified',
         'advisory_applicability_qualified', 'advisory_gate_qualified',
         'full_native_advisory_closure', 'bootstrap_environment_qualified',
         'signature_key_revocation_lifecycle_qualified', 'physical_qualified',
         'fips_validated', 'deployment_permitted')


def inputs():
    names = set(p.candidate.source_inputs()) | set(INPUTS)
    return {name: u.file_digest(ROOT / name) for name in sorted(names)}


def stanzas(stream):
    """Bound Sources records, retaining multiline checksums rather than folding them."""
    fields = {}; previous = None; total = paragraph = 0
    for raw in iter(lambda: stream.readline(32769), b''):
        total += len(raw); paragraph += len(raw)
        u.require(total <= MAX_INDEX and len(raw) <= 32768 and paragraph <= 512 * 1024,
                  'source-index-budget')
        u.require(b'\r' not in raw and b'\0' not in raw, 'source-index-control-byte')
        line = raw.decode('utf-8').rstrip('\n')
        if not line:
            if fields: yield fields
            fields = {}; previous = None; paragraph = 0
        elif line.startswith((' ', '\t')):
            u.require(previous is not None and previous not in {'package', 'version', 'directory'},
                      'continued-source-identity')
            fields[previous].append(line[1:])
        else:
            u.require(':' in line, 'source-index-field')
            name, value = line.split(':', 1); name = name.lower()
            u.require(re.fullmatch('[a-z][a-z0-9-]*', name) and name not in fields,
                      'duplicate-source-field')
            fields[name] = [value.strip()]; previous = name
    if fields: yield fields


def source_record(row, case):
    u.require(row['package'] == [case['source']] and row['version'] == [case['version']],
              'exact-source-identity')
    directory = row['directory']
    u.require(len(directory) == 1 and directory[0] == case['directory']
              and PurePosixPath(directory[0]).as_posix() == directory[0], 'exact-source-directory')
    lines = row['checksums-sha256']; u.require(lines[0] == '', 'source-checksum-header')
    files = {}
    for line in lines[1:]:
        fields = line.split(); u.require(len(fields) == 3, 'source-checksum-row')
        digest, size, name = fields
        u.require(re.fullmatch('[a-f0-9]{64}', digest) and size.isdecimal()
                  and 0 < int(size) <= p.MAX_ARCHIVE, 'source-file-identity')
        u.require(re.fullmatch('[A-Za-z0-9][A-Za-z0-9+._~-]{0,255}', name)
                  and PurePosixPath(name).name == name and name not in files,
                  'unique-source-basename')
        files[name] = dict(size=int(size), sha256=digest)
    u.require(set(files) == set(case['archives']) | {case['descriptor']}
              and len(files) <= 16 and files[case['descriptor']]['size'] <= 256 * 1024,
              'complete-reviewed-source-file-set')
    for name, expected in case['archives'].items():
        u.require(files[name] == expected, 'reviewed-source-bytes')
    return dict(source=case['source'], version=case['version'], directory=directory[0], files=files)


def select_source(stream, case):
    selected = None
    # Consume the complete authenticated index: a later duplicate cannot replace
    # the selected record, nor can malformed trailing data be silently ignored.
    for row in stanzas(stream):
        if row.get('package') == [case['source']] and row.get('version') == [case['version']]:
            u.require(selected is None, 'ambiguous-source-record')
            selected = source_record(row, case)
    return selected


def authenticate(evidence, trust, case):
    releases = []; indexes = []; selected = None
    now = datetime.now(timezone.utc)
    for suite in trust['suites']:
        folder, hashes, proof = p.verify_release(evidence, trust, suite, now)
        releases.append(proof)
        name = case['component'] + '/source/Sources.gz'
        u.require(name in hashes and 0 < hashes[name]['size'] <= p.MAX_ARCHIVE,
                  'signed-source-index-required')
        index = folder / 'Sources.gz'
        evidence.get(trust['archive'] + 'dists/' + suite + '/' + name, index,
                     p.MAX_ARCHIVE, hashes[name])
        p.changed_archive_control(index, hashes[name])
        index_proof = dict(suite=suite, index=name, **hashes[name],
                           release_sha256=proof['release_sha256'], changed_index_refused=True)
        indexes.append(index_proof)
        with gzip.open(index, 'rb') as stream: selected = select_source(stream, case)
        if selected is not None:
            selected['index'] = index_proof
            break
    u.require(selected is not None, 'exact-reviewed-source-unavailable')
    files = {}
    for name, identity in sorted(selected['files'].items()):
        archive = evidence.work / name
        evidence.get(trust['archive'] + selected['directory'] + '/' + name,
                     archive, p.MAX_ARCHIVE, identity)
        p.changed_archive_control(archive, identity)
        files[name] = dict(**identity, changed_archive_refused=True)
    return dict(signed_releases=releases, source_indexes=indexes, source_record=selected,
                authenticated_files=files, bad_signature_refusals=len(releases),
                changed_index_refusals=len(indexes), changed_archive_refusals=len(files))


def save(report, value, identity=None):
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    u.require(0 < len(raw) <= MAX_REPORT, 'source-receipt-budget')
    return u.save_report(report, value, identity)


def run(case_name, work, report):
    u.hosted()  # Before filesystem changes, tools, package queries or network.
    u.require(case_name in CASES, 'named-source-review-case')
    case = CASES[case_name]
    u.require(not os.path.lexists(work) and not os.path.lexists(report), 'fresh-source-work-and-report')
    head = u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    u.require(not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
              'clean-source-review-checkout')
    before = inputs(); trust = u.parse((ROOT / p.POLICY).read_bytes())
    p.policy(trust, datetime.now(timezone.utc))
    work.mkdir(mode=0o700); (work / 'gnupg').mkdir(mode=0o700)
    value = dict(schema=1, scope=SCOPE, qualified=False, verification_status='pending',
                 checkout=dict(clean=True, commit=head), inputs=before, case=case_name,
                 reviewed_source=case, archive_trust_policy_sha256=u.file_digest(ROOT / p.POLICY),
                 signed_source_index_authenticated=False, source_payloads_authenticated=False,
                 **dict.fromkeys(FLAGS, False))
    identity = save(report, value)
    try:
        evidence = p.Evidence(work)
        keyring = work / 'gnupg' / 'archive-keyring.gpg'
        captured = p.capture_keyring(p.KEYRING, keyring)
        proof = authenticate(evidence, trust, case)
        u.require(inputs() == before and u.file_digest(p.KEYRING) == captured['sha256']
                  and u.file_digest(keyring) == captured['sha256']
                  and stat.S_ISREG(keyring.lstat().st_mode)
                  and stat.S_IMODE(keyring.lstat().st_mode) == 0o400
                  and keyring.lstat().st_uid == os.getuid()
                  and u.command(['/usr/bin/git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == head
                  and not u.command(['/usr/bin/git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
                  'unchanged-source-proof-inputs')
        value.update(qualified=True, verification_status='complete',
                     signed_source_index_authenticated=True, source_payloads_authenticated=True,
                     archive_keyring_sha256=captured['sha256'], archive_keyring_source=captured['source'],
                     archive_keyring_snapshot=captured['snapshot'], **proof)
    except Exception as error:
        category = str(error) if isinstance(error, u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        value.update(qualified=False, verification_status='failed', failure_category=category)
        save(report, value, identity); raise
    save(report, value, identity)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try: run(args.case, args.work, args.report)
    except Exception as error:
        category = str(error) if isinstance(error, u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}', str(error)) else type(error).__name__
        parser.exit(1, 'Ubuntu source provenance refused (' + category + ').\n')
    print('PASS: signed Ubuntu source payloads; applicability and release admission remain open')


if __name__ == '__main__': main()
