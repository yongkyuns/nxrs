#!/usr/bin/env python3
"""Route D2 v0.9.0 SVGs from declared node ports; do not reposition D2 nodes.

D2 owns topology, wording, styling and geometry. routes.json owns boundary ports
and routing corridors. Geometry and browser checks fail instead of hiding errors.
"""
from __future__ import annotations
import argparse, base64, copy, hashlib, html, json, re
from pathlib import Path
import xml.etree.ElementTree as ET
SVG='http://www.w3.org/2000/svg'; N={'s':SVG}
ET.register_namespace('',SVG); ET.register_namespace('xlink','http://www.w3.org/1999/xlink')
def ident(g): return html.unescape(base64.b64decode(g.get('class','').split()[0]).decode())
def edges(root): return [g for g in root.iter(f'{{{SVG}}}g') if g.find('./s:path[@class="connection"]',N) is not None]
def snapshots(root): return {g.get('class'):ET.tostring(g) for g in root.iter(f'{{{SVG}}}g') if g.find('./s:g[@class="shape"]',N) is not None}
def topology(s):
 p,r=s.split('(',1);a,b=re.split(r' (?:<->|->|--|<-) ',r.rsplit(')',1)[0]);return p+a,p+b
def boxes(root):
 return {ident(g):tuple(float(r.get(t)) for t in ('x','y','width','height')) for g in root.iter(f'{{{SVG}}}g') if (r:=g.find('./s:g[@class="shape"]/s:rect',N)) is not None}
def port(box,spec):
 x,y,w,h=box;side,f=spec
 if not 0<=f<=1:raise ValueError('Port fraction outside boundary')
 return {'n':(x+w*f,y),'s':(x+w*f,y+h),'w':(x,y+h*f),'e':(x+w,y+h*f)}[side]
def points(c,bs):
 a=port(bs[c['source']],c['from']);b=port(bs[c['target']],c['to']);m=c['mode'];o=c.get('offset',0)
 if m=='straight': ps=[a,b]
 elif m=='mid-x':
  x=(a[0]+b[0])/2+o;ps=[a,(x,a[1]),(x,b[1]),b]
 elif m=='outside-left':
  x=min(a[0],b[0])-o;ps=[a,(x,a[1]),(x,b[1]),b]
 elif m=='mid-y':
  y=(a[1]+b[1])/2+o;ps=[a,(a[0],y),(b[0],y),b]
 elif m=='waypoints': ps=[a]+c['waypoints']+[b]
 else:raise ValueError('Unknown route mode '+m)
 out=[]
 for p in ps:
  if not out or p!=out[-1]:out.append(p)
 compact=[]
 for p in out:
  while len(compact)>=2 and ((compact[-2][0]==compact[-1][0]==p[0]) or (compact[-2][1]==compact[-1][1]==p[1])):compact.pop()
  compact.append(p)
 out=compact
 for p,q in zip(out,out[1:]):
  if p[0]!=q[0] and p[1]!=q[1]:raise ValueError(f'Non-orthogonal route {c}: {out}')
 return out

def intersects(a,b,box,pad=0):
 x,y,w,h=box;x-=pad;y-=pad;w+=2*pad;h+=2*pad
 if a[0]==b[0]:return x+1e-6<a[0]<x+w-1e-6 and max(min(a[1],b[1]),y)<min(max(a[1],b[1]),y+h)
 return y+1e-6<a[1]<y+h-1e-6 and max(min(a[0],b[0]),x)<min(max(a[0],b[0]),x+w)
def wire_metrics(routes):
 seg=[(i,a,b) for i,c in enumerate(routes) for a,b in zip(c['points'],c['points'][1:])];crossings=set();overlaps=[]
 for i,(eid,a,b) in enumerate(seg):
  for fid,c,d in seg[i+1:]:
   if eid==fid:continue
   av=a[0]==b[0];cv=c[0]==d[0]
   if av!=cv:
    v,w,h,k=(a,b,c,d) if av else (c,d,a,b);x,y=v[0],h[1]
    if min(v[1],w[1])<y<max(v[1],w[1]) and min(h[0],k[0])<x<max(h[0],k[0]):crossings.add((eid,fid,x,y))
   elif (av and a[0]==c[0]) or (not av and a[1]==c[1]):
    k=1 if av else 0;length=min(max(a[k],b[k]),max(c[k],d[k]))-max(min(a[k],b[k]),min(c[k],d[k]))
    if length>1e-6:overlaps.append({'routes':[eid,fid],'length':length})
 return [list(c) for c in sorted(crossings)],overlaps

def route(source,target,cfg):
 root=ET.parse(source).getroot();before=snapshots(root);bs=boxes(root)
 leaf={k:b for k,b in bs.items() if not any(k2.startswith(k+'.') for k2 in bs)}
 es=edges(root);index={topology(ident(g)):g for g in es}
 if len(index)!=len(es) or set(index)!={(c['source'],c['target']) for c in cfg}:raise ValueError('D2 topology does not match routes.json')
 collisions=[];actual=[]
 for c in cfg:
  g=index[c['source'],c['target']];p=g.find('./s:path[@class="connection"]',N);pts=points(c,bs)
  markers=p.get('marker-start'),p.get('marker-end');p.set('d','M '+' L '.join(f'{x:g},{y:g}' for x,y in pts));p.attrib.pop('mask',None)
  p.set('stroke-linejoin','round');p.set('stroke-linecap','round')
  # Do not paint over intersections: actual wire crossings are tested explicitly.
  for a,b in zip(pts,pts[1:]):
   for key,box in leaf.items():
    if intersects(a,b,box):collisions.append({'edge':[c['source'],c['target']],'node':key,'segment':[a,b]})
  for r in list(g.findall('./s:rect',N)):g.remove(r)
  t=g.find('./s:text',N)
  if t is not None:
   old=' '.join(t.itertext()).strip();segs=list(zip(pts,pts[1:]));idx=c.get('label_segment')
   if idx is None:idx=max(range(len(segs)),key=lambda i: abs(segs[i][1][0]-segs[i][0][0])+abs(segs[i][1][1]-segs[i][0][1]))
   a,b=segs[idx];x=(a[0]+b[0])/2;y=(a[1]+b[1])/2;side=c.get('label_side',-1)
   if a[1]==b[1]: y+=(-12 if side<0 else 27);anchor='middle'
   else:x+=(-12 if side<0 else 12);y+=7;anchor='end' if side<0 else 'start'
   dx,dy=c.get('label_offset',[0,0]);x+=dx;y+=dy
   if 'label' in c:x,y,anchor=c['label']
   st=re.sub(r'text-anchor:[^;]+',f'text-anchor:{anchor}',t.get('style',''))
   t.set('x',str(x));t.set('y',str(y));t.set('style',st)
   for child in list(t):t.remove(child)
   t.text=old
  if markers!=(p.get('marker-start'),p.get('marker-end')):raise ValueError('Arrow direction changed')
  actual.append({'source':c['source'],'target':c['target'],'points':pts})
 if snapshots(root)!=before:raise ValueError('Node geometry, style or wording changed')
 # Trim obsolete automatic-routing margins without translating any node.
 inner=root.find('./s:svg',N)
 if inner is not None:
  old=[float(x) for x in inner.get('viewBox').split()];left=-80 if any(c['mode']=='outside-left' and c['label_side']<0 for c in cfg) else -25
  right=max(x+w for x,y,w,h in bs.values())+25;width=right-left;height=old[3]
  root.set('viewBox',f'0 0 {width:g} {height:g}');inner.set('viewBox',f'{left:g} -25 {width:g} {height:g}');inner.set('width',f'{width:g}');inner.set('height',f'{height:g}')
 target.parent.mkdir(parents=True,exist_ok=True);ET.ElementTree(root).write(target,encoding='utf-8',xml_declaration=True)
 crossings,overlaps=wire_metrics(actual)
 return {'diagram':source.stem,'node_groups_preserved':len(before),'connectors':len(cfg),'boundary_ports':len(cfg)*2,'block_collisions':collisions,'wire_crossings':crossings,'collinear_overlaps':overlaps,'routes':actual,'raw_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'svg_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--input-dir',type=Path,required=True);ap.add_argument('--output-dir',type=Path,required=True);ap.add_argument('--routes',type=Path,default=Path(__file__).with_name('routes.json'));ap.add_argument('--prefix',default='');ap.add_argument('--report-only',action='store_true');a=ap.parse_args()
 spec=json.loads(a.routes.read_text());rs=[route(a.input_dir/(a.prefix+name+'.svg'),a.output_dir/(name+'.svg'),cfg) for name,cfg in spec['diagrams'].items()]
 (a.output_dir/'routing-checks.json').write_text(json.dumps({'renderer':spec['renderer'],'diagrams':rs},indent=2)+'\n')
 errors=[(r['diagram'],r['block_collisions'],r['wire_crossings'],r['collinear_overlaps']) for r in rs if r['block_collisions'] or r['wire_crossings'] or r['collinear_overlaps']]
 print(json.dumps(errors,indent=2))
 if errors and not a.report_only:raise SystemExit('Review connector geometry; do not refresh validation blindly')
if __name__=='__main__':main()
