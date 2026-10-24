#!/usr/bin/env python3
"""Verify a pinned MediaMTX executable and its unsigned public evidence bundle."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from media_engine import (Failure, RECIPE, ROOT, TESTS, MODULE, bounded,
                          digest, module_digest, require, source_inputs)


def verify(binary, *, qualification=False):
    recipe_raw=bounded(RECIPE,65536);recipe=json.loads(recipe_raw)['mediamtx']
    sha=digest(bounded(binary,64*1024*1024))
    provenance=json.loads(bounded(Path(str(binary)+'.provenance.json'),1024*1024))
    require(provenance.get('_type')=='https://in-toto.io/Statement/v1'
            and provenance.get('predicateType')=='https://slsa.dev/provenance/v1'
            and provenance.get('subject')==[dict(name=binary.name,digest=dict(sha256=sha))],
            'MediaMTX provenance subject')
    definition=provenance['predicate']['buildDefinition'];target=definition['externalParameters']['target']
    require(recipe['artifacts'][target]['sha256']==sha
            and definition['externalParameters']['recipe_sha256']==digest(recipe_raw)
            and definition['internalParameters']==dict(build=recipe['build'],runtime_version=recipe['version']),
            'MediaMTX recipe/artifact/configuration')
    materials=definition['resolvedDependencies'];before=source_inputs()
    expected=[dict(uri='nxrs:'+name,digest=dict(sha256=value)) for name,value in before.items()]
    expected += [dict(uri=pin['url'],digest=dict(sha256=pin['sha256'])) for pin in [recipe['source'],recipe['asset']]]
    require(all(value in materials for value in expected) and
            any(dict(uri=pin['url'],digest=dict(sha256=pin['sha256'])) in materials
                for pin in recipe['toolchains'].values()), 'MediaMTX complete source/module/SDK/asset materials')
    details=provenance['predicate']['runDetails'];payloads={};hashes={}
    for suffix in ['.cdx.json','.advisories.json']:
        raw=bounded(Path(str(binary)+suffix),1024*1024);hashes[suffix]=digest(raw)
        descriptor=dict(name=binary.name+suffix,mediaType='application/json',digest=dict(sha256=digest(raw)))
        require(descriptor in details['byproducts'],'bound MediaMTX inventory/advisories')
        payloads[suffix]=json.loads(raw)
    summary=payloads['.advisories.json'];scans=summary['scans']
    require(len(scans)==2 and {value['mode'] for value in scans}=={'source','binary'} and
            all(value['scanner']==recipe['scanner']['version'] and value['database']=='https://vuln.go.dev'
                and value['database_modified'] and value['imported_or_called']==0 and value['affected_ids']==[]
                for value in scans), 'passed MediaMTX source/binary advisories')
    main=summary['main_module'];review=recipe['main_module_review'];today=datetime.now(timezone.utc).date()
    require(main['module']==MODULE and main['version']==recipe['source']['version'] and main['affected'] is False
            and main['review_expires']==review['expires'] and today<datetime.fromisoformat(review['expires']).date()<=today+timedelta(days=90)
            and main['reviewed_advisories']=={p['id']:p['sha256'] for p in review['advisories']},
            'MediaMTX current upstream version review')
    require(summary['scanner_module']==recipe['scanner'] and
            re.fullmatch('[0-9a-f]{64}',summary['scanner_binary_sha256']) and
            summary['source_archive_sha256']==recipe['source']['sha256'] and
            summary['embedded_asset_sha256']==recipe['asset']['content_sha256'] and
            all(summary[key] is False for key in ['hls_javascript_advisories_qualified',
                'os_advisories_qualified','fips_validated','physical_qualified','deployment_permitted']),
            'MediaMTX audit identity and scope')
    controls=summary['qualification_controls']
    require(type(controls['target_executed']) is bool, 'explicit target execution scope')
    if qualification:
        require(controls['affected_binary_refused'] is True and
                controls['affected_binary_sha256']==recipe['negative_control'][target]['binary_sha256'] and
                {'GO-2026-6809','GO-2026-6603'}.issubset(controls['affected_advisory_ids']),
                'actual MediaMTX negative control required')
        if controls['target_executed']:
            require(controls['upstream_auth_tests']==sorted(TESTS),'upstream MediaMTX auth execution')
        else:require(controls['upstream_auth_tests']==[],'cross-build execution scope')
    bom=payloads['.cdx.json'];require(bom['bomFormat']=='CycloneDX' and bom['specVersion']=='1.6','MediaMTX inventory format')
    components=bom['components'];engine=[c for c in components if c['bom-ref']=='engine'];asset=[c for c in components if c['bom-ref']=='hls-js']
    require(len(engine)==len(asset)==1 and engine[0]['hashes']==[dict(alg='SHA-256',content=sha)] and
            engine[0]['version']==recipe['version'] and asset[0]['version']==recipe['asset']['version'] and
            asset[0]['hashes']==[dict(alg='SHA-256',content=recipe['asset']['content_sha256'])], 'MediaMTX inventory subjects')
    standard=[c for c in components if c['bom-ref']=='stdlib']
    require(len(standard)==1 and standard[0]['version']==recipe['go_version'],'compiled Go inventory')
    mod=bounded(ROOT/'deploy/mediamtx/go.mod',1024*1024).decode()
    sums=set(bounded(ROOT/'deploy/mediamtx/go.sum',1024*1024).decode().splitlines())
    compiled=[]
    for c in components:
        if c['bom-ref'] in ['engine','stdlib','hls-js']:continue
        name,version=c['name'],c['version'];properties={v['name']:v['value'] for v in c['properties']}
        require(re.search(r'^\s*'+re.escape(name)+' '+re.escape(version)+r'(?:\s|$)',mod,re.M)
                and name+' '+version+' '+properties['nxrs:go:module-sum'] in sums, 'compiled MediaMTX module identity')
        compiled.append([name,version,properties['nxrs:go:module-sum']])
    require(module_digest(compiled)==recipe['artifacts'][target]['modules_sha256'] and
            len(compiled)==recipe['artifacts'][target]['module_count'], 'complete MediaMTX compiled inventory')
    refs=[c['bom-ref'] for c in components]
    require(len(refs)==len(set(refs)) and len(refs)>3 and bom['dependencies']==[
            dict(ref='engine',dependsOn=[r for r in refs if r!='engine'])],'MediaMTX component graph')
    return dict(artifact=target,sha256=sha,components=len(components),unsigned=True,
                advisory_sha256=hashes['.advisories.json'],inventory_sha256=hashes['.cdx.json'])


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary',type=Path)
    parser.add_argument('--require-qualification',action='store_true');args=parser.parse_args()
    try:value=verify(args.binary,qualification=args.require_qualification)
    except (Failure,OSError,ValueError,KeyError,TypeError):parser.exit(1,'MediaMTX artifact/evidence verification refused.\n')
    print('PASS: exact public MediaMTX artifact and unsigned evidence',json.dumps(value,sort_keys=True))

if __name__=='__main__':main()
