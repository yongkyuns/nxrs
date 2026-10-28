#!/usr/bin/env python3
"""Assess one reviewed architecture condition after exact package authentication.

Raw scan and payload receipts remain immutable. This is a separate, expiring
assessment of the inventoried images, never a complete release/OS/FIPS claim.
"""
import argparse
import copy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import payloads

u=payloads.u
candidate=payloads.candidate
ROOT=u.ROOT
POLICY='security/ubuntu-native-applicability.json'
SCOPE='hosted-authenticated-ubuntu-native-advisory-applicability'
ADVISORY='UBUNTU-CVE-2026-97399'
RECORD='5bbff9ba4d7538934f7a020bcf2d11dde3917f75544403b01cf21e26510921ee'
MODIFIED='2026-10-01T19:11:28.548573898Z'
IMAGES={'/usr/lib/x86_64-linux-gnu/'+name for name in ('libc.so.6','libm.so.6','ld-linux-x86-64.so.2')}
FLAGS=('full_native_advisory_closure','package_payload_authenticated','custom_source_payload_authenticated',
       'kernel_qualified','firmware_qualified','physical_qualified','fips_validated','deployment_permitted')


def require(value,category):u.require(value,category)


def current_time():return datetime.now(timezone.utc)


def instant(value):
    require(type(value) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z',value),'review-time-format')
    try:return datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise u.Refusal('review-time-value') from None


def policy(value,now):
    require(type(value) is dict and set(value)=={'schema','scope','review'} and type(value['schema']) is int
            and value['schema']==1 and value['scope']==SCOPE,'native-applicability-policy')
    rule=value['review']
    if rule is None:return None
    require(type(rule) is dict and set(rule)=={'id','advisory','canonical_record_sha256','modified','source_package',
        'source_version','binary_package','package_sha256','reviewed_at','valid_until','references','images'},'exact-native-review-fields')
    # This validator admits one reviewed condition, not arbitrary CVE waivers.
    # A different advisory or changed advisory semantics needs a code review.
    require(rule['id']=='glibc-power8-strncasecmp-amd64-20261027' and rule['advisory']==ADVISORY
        and rule['canonical_record_sha256']==RECORD and rule['modified']==MODIFIED
        and rule['source_package']=='glibc' and rule['source_version']=='2.39-0ubuntu8.9'
        and rule['binary_package']=='libc6:amd64','reviewed-power8-condition-only')
    require(type(rule['package_sha256']) is str and re.fullmatch('[a-f0-9]{64}',rule['package_sha256']),'review-package-hash')
    require(rule['references']==['https://ubuntu.com/security/CVE-2026-97399',
        'https://www.openwall.com/lists/oss-security/2026/09/28/7'],'review-primary-references')
    start,end=instant(rule['reviewed_at']),instant(rule['valid_until'])
    require(isinstance(now,datetime) and now.tzinfo is not None and start<=now<end
        and 0<(end-start).total_seconds()<=31*86400,'current-expiring-native-review')
    require(type(rule['images']) is dict and set(rule['images'])==IMAGES,'complete-reviewed-glibc-images')
    for row in rule['images'].values():
        require(type(row) is dict and set(row)=={'sha256','size','elf'} and type(row['sha256']) is str
            and re.fullmatch('[a-f0-9]{64}',row['sha256']) and type(row['size']) is int
            and 64<=row['size']<=u.MAX_IMAGE,'review-image-identity')
        require(row['elf']==dict(bits=64,byte_order='little',machine=62,type=3,osabi=3,abi_version=0,flags=0)
            and all(type(row['elf'][key]) is int for key in ('bits','machine','type','osabi','abi_version','flags')),
            'reviewed-amd64-elf-identity')
    return rule


def authenticated(parent,proof):
    require(type(proof['schema']) is int and proof['schema']==2 and proof['scope']==payloads.SCOPE
        and proof['qualified'] is True and proof['verification_status']=='complete'
        and proof['distribution_payloads_authenticated'] is True,'authenticated-payload-assessment-required')
    require(proof['checkout']==parent['checkout'] and proof['inputs']==parent['inputs']
        and proof['advisory_gate_qualified'] is parent['qualified']
        and proof['source_advisory_findings']==parent['source_advisory_findings']
        and type(proof['advisory_findings']) is int
        and proof['advisory_findings']==len(parent['findings'])+len(parent['source_advisory_findings']),
        'same-authenticated-candidate-assessment')
    inventory=parent['inventory'];selected={n:r for n,r in inventory['files'].items() if 'binary_package' in r}
    require(set(proof['files'])==set(selected) and set(proof['packages'])==set(inventory['packages']),
        'complete-authenticated-image-and-package-sets')
    for name,row in selected.items():
        matched=proof['files'][name]
        require(row['binary_package'] in proof['packages'] and row['sha256']==matched['sha256']
            and row['size']==matched['size'] and row['binary_package']==matched['binary_package']
            and matched['archive_sha256']==proof['packages'][row['binary_package']]['sha256'],
            'same-authenticated-image-bytes')
    for name,row in inventory['packages'].items():
        require(all(row[key]==proof['packages'][name][key] for key in ('name','version','architecture','source','source_version')),
            'same-authenticated-package-identities')


def review_images(inventory,proof,rule):
    packages={n:r for n,r in inventory['packages'].items() if r['source']=='glibc'}
    if set(packages)!={rule['binary_package']}:return 'glibc-package-scope-changed'
    selected=packages[rule['binary_package']]
    if selected['architecture']!='amd64' or selected['source_version']!=rule['source_version']:
        return 'glibc-package-version-or-architecture-changed'
    if proof['packages'][rule['binary_package']]['sha256']!=rule['package_sha256']:return 'glibc-package-payload-changed'
    images={n:r for n,r in inventory['files'].items() if r.get('binary_package') in packages}
    if set(images)!=set(rule['images']):return 'glibc-image-scope-changed'
    if any(u.canonical_record_digest({key:row[key] for key in ('sha256','size','elf')})!=u.canonical_record_digest(rule['images'][name])
            for name,row in images.items()):
        return 'glibc-image-identity-changed'
    return None


def assess(parent,proof,configuration,now):
    authenticated(parent,proof)
    require(type(parent['findings']) is list and len(parent['findings'])<=2048
        and type(parent['source_advisory_findings']) is list and len(parent['source_advisory_findings'])<=2048,'bounded-raw-advisory-findings')
    rule=policy(configuration,now);reviewed=[];blocking=[];rejected=[];seen=set()
    for row in parent['findings']:
        key=(row['source_package'],row['advisory'])
        require(key not in seen and row['source_version']==parent['inventory']['source_packages'][row['source_package']],
            'unique-current-advisory-identities');seen.add(key)
        if rule is None or row['advisory']!=rule['advisory']:
            blocking.append(row);continue
        reason=None
        if (row['source_package']!=rule['source_package'] or row['source_version']!=rule['source_version']
                or row.get('canonical_record_sha256')!=rule['canonical_record_sha256'] or row['modified']!=rule['modified']):
            reason='advisory-record-or-source-changed'
        else:reason=review_images(parent['inventory'],proof,rule)
        if reason:
            blocking.append(row);rejected.append(dict(advisory=row['advisory'],reason=reason));continue
        reviewed.append(dict(finding=row,review_id=rule['id'],valid_until=rule['valid_until'],
            disposition='power8-only-condition-absent-from-exact-amd64-images',images=rule['images'],
            binary_package=rule['binary_package'],package_sha256=rule['package_sha256'],references=rule['references']))
    retained=parent['source_advisory_findings']
    return copy.deepcopy(dict(raw_distribution_findings=parent['findings'],raw_source_findings=retained,
        reviewed_findings=reviewed,rejected_reviews=rejected,blocking_distribution_findings=blocking,
        blocking_source_findings=retained,raw_advisory_gate_qualified=parent['qualified'],
        qualified=not blocking and not retained))


def run(candidate_path,payload_path,report):
    u.hosted() # Before filesystem changes, native commands or network.
    require(not os.path.lexists(report),'new-applicability-report')
    raw,parent=candidate.read(candidate_path);praw,proof=candidate.read(payload_path);cfgraw,configuration=candidate.read(ROOT/POLICY)
    head=u.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip();inputs=candidate.source_inputs()
    require(not u.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-applicability-source')
    require(parent['checkout']==dict(clean=True,commit=head) and parent['inputs']==inputs
        and parent['advisory_status']=='complete' and proof['candidate_report_sha256']==u.digest(raw)
        and proof['archive_trust_policy_sha256']==u.file_digest(ROOT/payloads.POLICY),'current-applicability-parents')
    require(set(parent['inventory']['roots'])==set(u.SYSTEM_ROOTS)|set(u.REPOSITORY_ROOTS),'complete-applicability-root-set')
    candidate.validate_advisory_result(parent,head)
    result=dict(schema=1,scope=SCOPE,qualified=False,assessment_status='pending',checkout=dict(clean=True,commit=head),inputs=inputs,
        candidate_report_sha256=u.digest(raw),payload_report_sha256=u.digest(praw),review_policy_sha256=u.digest(cfgraw),
        raw_distribution_findings=parent['findings'],raw_source_findings=parent['source_advisory_findings'],
        reviewed_findings=[],rejected_reviews=[],blocking_distribution_findings=parent['findings'],
        blocking_source_findings=parent['source_advisory_findings'],raw_advisory_gate_qualified=parent['qualified'],
        **dict.fromkeys(FLAGS,False))
    identity=u.save_report(report,result)
    try:
        assessment=assess(parent,proof,configuration,current_time())
        for reviewed in assessment['reviewed_findings']:
            for name,expected in reviewed['images'].items():
                require(u.image_identity(Path(name))==expected,'unchanged-reviewed-image-header-and-bytes')
                actual=payloads.installed(Path(name),expected['sha256'])
                require(all(proof['files'][name][key]==value for key,value in actual.items()),'unchanged-authenticated-reviewed-image')
        require(candidate.read(candidate_path)[0]==raw and candidate.read(payload_path)[0]==praw
            and candidate.read(ROOT/POLICY)[0]==cfgraw and candidate.source_inputs()==inputs
            and u.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()==head
            and not u.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-applicability-inputs')
        checked=current_time();policy(configuration,checked)
        result.update(**assessment,assessment_status='complete',checked_on=checked.isoformat())
    except Exception as error:
        category=str(error) if isinstance(error,u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        result.update(qualified=False,assessment_status='failed',failure_category=category);u.save_report(report,result,identity);raise
    u.save_report(report,result,identity);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('candidate','payload','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    try:result=run(args.candidate,args.payload,args.report)
    except Exception as error:
        category=str(error) if isinstance(error,u.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        parser.exit(1,'Native applicability assessment refused ('+category+').\n')
    count=len(result['blocking_distribution_findings'])+len(result['blocking_source_findings'])
    print('Native applicability: reviewed='+str(len(result['reviewed_findings']))+' blocking='+str(count),flush=True)
    raise SystemExit(0 if result['qualified'] else 1)


if __name__=='__main__':main()
