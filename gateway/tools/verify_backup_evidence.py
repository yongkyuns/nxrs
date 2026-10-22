#!/usr/bin/env python3
"""Verify a public native build bundle against this checkout's reviewed recipe.

Integrity/coherence check only: unsigned local records do not authenticate a
release publisher, establish SLSA certification or constitute a product SBOM.
"""
import argparse
import json
from pathlib import Path
import re
from backup_engine import Failure, RECIPE, ROOT, bounded, digest, require


def verify(binary, *, qualification=False):
    recipe_bytes=bounded(RECIPE,65536);recipe=json.loads(recipe_bytes)
    sha=digest(bounded(binary,64*1024*1024))
    provenance=json.loads(bounded(Path(str(binary)+'.provenance.json'),1024*1024))
    require(provenance['_type']=='https://in-toto.io/Statement/v1'
            and provenance['predicateType']=='https://slsa.dev/provenance/v1'
            and provenance['subject']==[dict(name=binary.name,digest=dict(sha256=sha))],
            'artifact provenance subject')
    definition=provenance['predicate']['buildDefinition'];target=definition['externalParameters']['target']
    require(recipe['artifacts'][target]['sha256']==sha
            and definition['externalParameters']['recipe_sha256']==digest(recipe_bytes)
            and definition['internalParameters']['build']==recipe['build'],'reviewed recipe and artifact')
    materials=definition['resolvedDependencies']
    expected=[dict(uri=recipe['source']['url'],digest=dict(sha256=recipe['source']['sha256'])),
              dict(uri='nxrs:backup-dependencies.json',digest=dict(sha256=digest(recipe_bytes))),
              dict(uri='nxrs:gateway/tools/backup_engine.py',digest=dict(sha256=digest(bounded(ROOT/'tools/backup_engine.py',1024*1024)))),
              *[dict(uri='nxrs:'+pin['path'],digest=dict(sha256=pin['sha256'])) for pin in recipe['modules'].values()]]
    require(all(value in materials for value in expected)
            and any(dict(uri=pin['url'],digest=dict(sha256=pin['sha256'])) in materials
                    for pin in recipe['toolchains'].values()),'complete source/module/SDK/builder materials')
    details=provenance['predicate']['runDetails']
    payloads={}
    for suffix in ['.cdx.json','.advisories.json']:
        data=bounded(Path(str(binary)+suffix),1024*1024)
        descriptor=dict(name=binary.name+suffix,mediaType='application/json',digest=dict(sha256=digest(data)))
        require(descriptor in details['byproducts'],'bound public inventory/advisory report')
        payloads[suffix]=json.loads(data)
    summary=payloads['.advisories.json'];scans=summary['scans']
    require(len(scans)==2 and {v['mode'] for v in scans}=={'source','binary'}
            and all(v['scanner']==recipe['scanner']['version'] and v['database']=='https://vuln.go.dev'
                    and v['database_modified'] and v['imported_or_called']==0 and v['affected_ids']==[] for v in scans)
            and summary['upstream']['module']=='github.com/restic/restic'
            and summary['upstream']['matching_entries']==0
            and summary['scanner_module']==recipe['scanner'], 'passed native advisory evidence')
    require(not qualification or summary['qualification_controls'] is True,'executed native qualification controls')
    bom=payloads['.cdx.json']
    require(bom['bomFormat']=='CycloneDX' and bom['specVersion']=='1.6','native inventory format')
    components=bom['components'];engine=[c for c in components if c['bom-ref']=='engine']
    require(len(engine)==1 and engine[0]['hashes']==[dict(alg='SHA-256',content=sha)],'native inventory subject')
    mod=bounded(ROOT/'deploy/restic/go.mod',1024*1024).decode()
    sums=set(bounded(ROOT/'deploy/restic/go.sum',1024*1024).decode().splitlines())
    for component in components:
        if component['bom-ref'] in ['engine','stdlib']:continue
        name,version=component['name'],component['version']
        require(re.search(r'^\s*'+re.escape(name)+' '+re.escape(version)+r'(?:\s|$)',mod,re.M),'inventory module version')
        props={v['name']:v['value'] for v in component['properties']}
        require(name+' '+version+' '+props['nxrs:go:module-sum'] in sums,'inventory module content identity')
    refs=[v['bom-ref'] for v in components]
    require(len(refs)==len(set(refs)) and len(refs)>2 and bom['dependencies']==[
            dict(ref='engine',dependsOn=[r for r in refs if r!='engine'])],'inventory component scope')
    return dict(artifact=target,sha256=sha,components=len(components),unsigned=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary',type=Path)
    parser.add_argument('--require-qualification',action='store_true');args=parser.parse_args()
    try:result=verify(args.binary,qualification=args.require_qualification)
    except (Failure,OSError,ValueError,KeyError,TypeError):
        parser.exit(1,'Native artifact/evidence integrity verification failed.\n')
    print('PASS: exact public artifact and unsigned evidence bundle',json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
