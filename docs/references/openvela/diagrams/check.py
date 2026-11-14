#!/usr/bin/env python3
"""Check atlas topology coverage, deterministic output and recorded geometry."""
from pathlib import Path
import hashlib,json,sys,xml.etree.ElementTree as ET
p=Path(__file__).parent
spec=json.loads((p/'routes.json').read_text());names=set(spec['diagrams'])
assert {x.stem for x in p.glob('*.d2') if x.stem!='material'}==names,'Unregistered D2 source'
assert {x.stem for x in p.glob('*.svg')}==names,'Missing/stale SVG'
rs=json.loads((p/'routing-checks.json').read_text())['diagrams']
assert len(rs)==len(names),'Incomplete geometry report'
for r in rs:
 name=r['diagram'];assert name in names
 assert not (r['block_collisions'] or r['wire_crossings'] or r['collinear_overlaps']),name
 assert r['connectors']==len(spec['diagrams'][name]) and r['boundary_ports']==2*r['connectors']
 svg=p/(name+'.svg');assert hashlib.sha256(svg.read_bytes()).hexdigest()==r['svg_sha256'],name+' stale report'
 root=ET.parse(svg).getroot();_,_,w,h=map(float,root.get('viewBox').split())
 assert w>h and w<=1650,(name,w,h)
print(f'{len(names)} SVGs: topology, hashes, ports, clear routes and landscape aspect verified')
