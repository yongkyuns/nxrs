#!/usr/bin/env python3
"""Route D2 SVG connectors without changing node or container geometry.

Use after the pinned D2 v0.9.0 render. Routes are explicit, separately editable
waypoints in routes.json. D2 remains the source of graph topology and styling.
This is a post-render routing pass, not a second automatic block layout.
"""
from __future__ import annotations
import argparse, base64, copy, hashlib, html, json, re
from pathlib import Path
import xml.etree.ElementTree as ET

SVG='http://www.w3.org/2000/svg'
ET.register_namespace('',SVG)
ET.register_namespace('xlink','http://www.w3.org/1999/xlink')
N={'s':SVG}

def edge_id(g: ET.Element)->str:
    return html.unescape(base64.b64decode(g.get('class','').split()[0]).decode())

def edges(root):
    return [g for g in root.iter(f'{{{SVG}}}g') if g.find('./s:path[@class="connection"]',N) is not None]

def node_snapshot(root):
    """Exact node positions, dimensions, styles, labels, and container groups."""
    return {g.get('class'):ET.tostring(g) for g in root.iter(f'{{{SVG}}}g')
            if g.find('./s:g[@class="shape"]',N) is not None}

def rounded_path(points,radius=6):
    if len(points)<2:raise ValueError('A route needs at least two points')
    pts=[(float(x),float(y)) for x,y in points]
    for a,b in zip(pts,pts[1:]):
        if a==b or (a[0]!=b[0] and a[1]!=b[1]):
            raise ValueError(f'Route must have nonzero orthogonal segments: {a}, {b}')
    fmt=lambda p:f'{p[0]:g},{p[1]:g}'
    d='M '+fmt(pts[0])
    for a,b,c in zip(pts,pts[1:],pts[2:]):
        if (a[0]==b[0]==c[0]) or (a[1]==b[1]==c[1]):
            d+=' L '+fmt(b);continue
        l1=abs(a[0]-b[0])+abs(a[1]-b[1]);l2=abs(c[0]-b[0])+abs(c[1]-b[1])
        r=min(radius,l1/2,l2/2)
        before=(b[0]+(a[0]-b[0])*r/l1,b[1]+(a[1]-b[1])*r/l1)
        after=(b[0]+(c[0]-b[0])*r/l2,b[1]+(c[1]-b[1])*r/l2)
        d+=' L '+fmt(before)+' Q '+fmt(b)+' '+fmt(after)
    return d+' L '+fmt(pts[-1])


def all_boxes(root):
    return {edge_id(g): tuple(float(r.get(t)) for t in ('x','y','width','height'))
            for g in root.iter(f'{{{SVG}}}g')
            if (r := g.find('./s:g[@class="shape"]/s:rect',N)) is not None}

def check_endpoint(point, box, name):
    """Attach to the declared node, not an unrelated raw ELK endpoint."""
    x,y,w,h=box;px,py=point;eps=1e-6
    in_bounds=x-eps<=px<=x+w+eps and y-eps<=py<=y+h+eps
    boundary=min(abs(px-x),abs(px-x-w),abs(py-y),abs(py-y-h))<=eps
    if not in_bounds or not boundary:
        raise ValueError(f'Route endpoint {point} is not on {name} boundary {box}')

def topology(edge):
    prefix,rest=edge.split('(',1)
    a,b=re.split(r' (?:<->|->|--|<-) ',rest.rsplit(')',1)[0])
    return prefix+a,prefix+b

def wire_metrics(configs):
    """Count crossings; do NOT claim that obstacle-free routing is planar."""
    segments=[(c['edge'],a,b) for c in configs if c.get('role')!='lifeline'
              for a,b in zip(c['points'],c['points'][1:])]
    crossings=set();overlaps=[]
    for i,(eid,a,b) in enumerate(segments):
        for fid,c,d in segments[i+1:]:
            if eid==fid:continue
            av=a[0]==b[0];cv=c[0]==d[0]
            if av!=cv:
                v0,v1,h0,h1=(a,b,c,d) if av else (c,d,a,b)
                x,y=v0[0],h0[1]
                if min(v0[1],v1[1])<y<max(v0[1],v1[1]) and min(h0[0],h1[0])<x<max(h0[0],h1[0]):
                    crossings.add((tuple(sorted((eid,fid))),x,y))
            elif (av and a[0]==c[0]) or (not av and a[1]==c[1]):
                k=1 if av else 0
                length=min(max(a[k],b[k]),max(c[k],d[k]))-max(min(a[k],b[k]),min(c[k],d[k]))
                if length>1e-6:overlaps.append({'edges':[eid,fid],'length':length})
    return len(crossings),overlaps

def leaf_boxes(root):
    shapes=[]
    for g in root.iter(f'{{{SVG}}}g'):
        sh=g.find('./s:g[@class="shape"]/s:rect',N)
        if sh is None or g.get('style')=='opacity:0':continue
        shapes.append((edge_id(g),sh))
    for k,r in shapes:
        if any(k2.startswith(k+'.') for k2,_ in shapes):continue
        if float(r.get('width'))<=2:continue
        yield k,tuple(float(r.get(t)) for t in ('x','y','width','height'))

def crosses(a,b,box):
    x,y,w,h=box
    # Test centerlines against strict leaf-box interiors, not container backgrounds.
    x+=1;y+=1;w-=2;h-=2
    if a[0]==b[0]:return x<a[0]<x+w and max(min(a[1],b[1]),y)<min(max(a[1],b[1]),y+h)
    return y<a[1]<y+h and max(min(a[0],b[0]),x)<min(max(a[0],b[0]),x+w)

def route_file(source:Path,target:Path,configs:list[dict]):
    root=ET.parse(source).getroot();before=node_snapshot(root)
    es=edges(root);index={edge_id(g):g for g in es}
    if len(index)!=len(es):raise ValueError('Duplicate edge identifiers')
    if set(index)!=set(c['edge'] for c in configs):
        raise ValueError('Routing specification does not match diagram topology')
    collisions=[];boxes=list(leaf_boxes(root));ports=all_boxes(root)
    for c in configs:
        g=index[c['edge']];p=g.find('./s:path[@class="connection"]',N)
        pts=c['points']
        if topology(c['edge'])!=(c['source'],c['target']):
            raise ValueError('Configured endpoints change D2 topology')
        check_endpoint(pts[0],ports[c['source']],c['source'])
        check_endpoint(pts[-1],ports[c['target']],c['target'])
        markers=(p.get('marker-start'),p.get('marker-end'))
        p.set('d',rounded_path(pts));p.attrib.pop('mask',None)
        p.set('stroke-linejoin','round');p.set('stroke-linecap','round')
        halo=copy.deepcopy(p)
        halo.set('class','routing-halo')
        halo.attrib.pop('marker-end',None);halo.attrib.pop('marker-start',None)
        halo.set('style','fill:none;stroke:white;stroke-width:7;stroke-linecap:round;stroke-linejoin:round')
        g.insert(0,halo)
        for a,b in zip(pts,pts[1:]):
            for key,box in boxes:
                if crosses(a,b,box):collisions.append({'edge':c['edge'],'node':key,'segment':[a,b]})

        for rect in list(g.findall('./s:rect',N)): g.remove(rect)
        label=c.get('label');t=g.find('./s:text',N)
        if label and t is not None:
            old=' '.join(t.itertext()).strip();lines=label.get('lines') or [old]
            if re.sub(r'\s+', '', old) != re.sub(r'\s+', '', ''.join(lines)):
                raise ValueError('Connector label content changed')
            size=label.get('font_size') or float(re.search(r'font-size:([\d.]+)',t.get('style','')).group(1))
            x=label['x'];y=label['y'];style=re.sub(r'font-size:[\d.]+px',f'font-size:{size:g}px',t.get('style',''))
            t.set('x',str(x));t.set('y',str(y));t.set('style',style)
            for child in list(t):t.remove(child)
            t.text=None
            for i,line in enumerate(lines):
                span=ET.SubElement(t,f'{{{SVG}}}tspan',{'x':str(x),'dy':'0' if i==0 else f'{size*1.18:g}'})
                span.text=line
            if c.get('label_background'):
                width=label.get('background_width',640)
                bg=ET.Element(f'{{{SVG}}}rect',{
                    'class':'routing-label-bg','x':str(x-width/2),'y':str(y-size-3),
                    'width':str(width),'height':str(size*1.18*len(lines)+8),
                    'fill':'#FAFAFA','stroke':'none'})
                g.insert(list(g).index(t),bg)
        if markers!=(p.get('marker-start'),p.get('marker-end')):
            raise AssertionError('Routing changed arrow direction')
    if node_snapshot(root)!=before:raise AssertionError('Routing changed a node group')
    target.parent.mkdir(parents=True,exist_ok=True)
    ET.ElementTree(root).write(target,encoding='utf-8',xml_declaration=True)
    crossing_count,overlaps=wire_metrics(configs)
    if overlaps:raise ValueError(f'Collinear signal routes overlap: {overlaps}')
    return {'name':source.stem,'node_groups_unchanged':len(before),'edges_routed':len(configs),
            'boundary_endpoints_checked':2*len(configs),'arrow_markers_preserved':True,
            'block_interior_intersections':collisions,'signal_wire_crossings':crossing_count,
            'collinear_signal_overlaps':overlaps}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input-dir',type=Path,required=True)
    ap.add_argument('--output-dir',type=Path,required=True)
    ap.add_argument('--routes',type=Path,default=Path(__file__).with_name('routes.json'))
    a=ap.parse_args();spec=json.loads(a.routes.read_text());reports=[]
    for name,cfg in spec['diagrams'].items():
        raw=(a.input_dir/(name+'.svg')).read_bytes()
        blob=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()
        if blob != spec['raw_svg_blobs'][name]:
            raise ValueError('Baseline SVG changed; review routes before regenerating '+name)
        reports.append(route_file(a.input_dir/(name+'.svg'),a.output_dir/(name+'.svg'),cfg))
    report={'baseline_commit':spec['baseline_commit'],'routing':reports}
    (a.output_dir/'routing-checks.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    if any(r['block_interior_intersections'] for r in reports):raise SystemExit('Routes intersect a node interior')
if __name__=='__main__':main()
