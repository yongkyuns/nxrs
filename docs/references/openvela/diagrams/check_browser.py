#!/usr/bin/env python3
"""Measure actual embedded-font text in Chromium, then export review PNGs.
Requires playwright and Chromium only for documentation validation.
"""
from pathlib import Path
import argparse,json,itertools
from playwright.sync_api import sync_playwright

def overlap(a,b,gap=1):
 return min(a['right'],b['right'])-max(a['left'],b['left'])>gap and min(a['bottom'],b['bottom'])-max(a['top'],b['top'])>gap

def inspect(directory,out,chromium):
 spec=json.loads((directory/'routes.json').read_text());report=[]
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=chromium,headless=True,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1600,'height':1200},device_scale_factor=1)
  for name in spec['diagrams']:
   svg=(directory/(name+'.svg')).read_text()
   page.set_content('<style>body{margin:0}svg{display:block}</style>'+svg)
   page.evaluate('document.fonts.ready')
   data=page.evaluate('''() => {
     const svg=document.querySelector('svg');
     let vb=svg.viewBox.baseVal;
     svg.style.width=vb.width+'px';svg.style.height=vb.height+'px';
     function box(el){let r=el.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}}
     const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');
     function glyphBox(el){
       let r=box(el),cs=getComputedStyle(el);ctx.font=cs.fontStyle+' '+cs.fontWeight+' '+cs.fontSize+' '+cs.fontFamily;
       let tm=ctx.measureText(el.textContent),p=el.getStartPositionOfChar(0),m=el.getScreenCTM();
       let baseline=m.b*p.x+m.d*p.y+m.f;
       r.top=baseline-tm.actualBoundingBoxAscent*m.d;r.bottom=baseline+tm.actualBoundingBoxDescent*m.d;r.height=r.bottom-r.top;
       return r;
     }
     let labels=Array.from(svg.querySelectorAll('text')).flatMap(t=>{
       let ts=t.querySelectorAll('tspan');return ts.length?Array.from(ts):[t]
     }).filter(t=>t.textContent.trim()).map(t=>{
       let g=t.closest('g');while(g && !g.classList.length)g=g.parentElement.closest('g');
       return {text:t.textContent.trim(),group:g?.getAttribute('class'),...glyphBox(t),size:parseFloat(getComputedStyle(t).fontSize)};
     });
     let nodes=Array.from(svg.querySelectorAll('g.shape')).filter(s=>s.querySelector('rect')).map(s=>({group:s.parentElement.getAttribute('class'),...box(s.querySelector('rect'))}));
     let paths=Array.from(svg.querySelectorAll('path.connection')).map(p=>{
       let m=p.getScreenCTM();return {d:p.getAttribute('d'),matrix:[m.a,m.b,m.c,m.d,m.e,m.f],group:p.parentElement.getAttribute('class')}
     });
     return {width:vb.width,height:vb.height,labels,nodes,paths};
   }''')
   errors=[]
   for a,b in itertools.combinations(data['labels'],2):
    if overlap(a,b):errors.append({'type':'text_overlap','a':a['text'],'b':b['text']})
   node_by={n['group']:n for n in data['nodes']}
   import base64,html
   def key(n):return html.unescape(base64.b64decode(n['group'].split()[0]).decode())
   leaf=[n for n in data['nodes'] if not any(key(other).startswith(key(n)+'.') for other in data['nodes'])]
   for t in data['labels']:
    # Container headings and leaf labels must remain inside their own rectangles.
    if n:=node_by.get(t['group']):
     if t['left']<n['left']-1 or t['right']>n['right']+1 or t['top']<n['top']-1 or t['bottom']>n['bottom']+1:errors.append({'type':'out_of_node','text':t['text'],'group':t['group']})
    if t['group'] not in node_by:
     for n in leaf:
      if overlap(t,n):errors.append({'type':'label_over_block','text':t['text'],'node':key(n)})
    if t['left']<0 or t['top']<0 or t['right']>data['width'] or t['bottom']>data['height']:errors.append({'type':'canvas_clip','text':t['text']})
   import re
   for path in data['paths']:
    nums=[float(v) for v in re.findall(r'-?\d+(?:\.\d+)?',path['d'])];pts=list(zip(nums[::2],nums[1::2]));m=path['matrix'];pts=[(m[0]*x+m[2]*y+m[4],m[1]*x+m[3]*y+m[5]) for x,y in pts]
    for a,b in zip(pts,pts[1:]):
     for t in data['labels']:
      # Include a 3px clearance around actual glyph bounds.
      x0,x1=t['left']-3,t['right']+3;y0,y1=t['top']-3,t['bottom']+3
      hit=(x0<a[0]<x1 and max(min(a[1],b[1]),y0)<min(max(a[1],b[1]),y1)) if a[0]==b[0] else (y0<a[1]<y1 and max(min(a[0],b[0]),x0)<min(max(a[0],b[0]),x1))
      if hit:errors.append({'type':'text_wire','text':t['text'],'edge':path['group']})
   rw=spec.get('reading_width',1400);minimum=min(t['size'] for t in data['labels'])*min(1,rw/data['width'])
   if minimum<14:errors.append({'type':'small_type','minimum_at_reading_width':minimum})
   out.mkdir(parents=True,exist_ok=True)
   page.locator('svg').first.screenshot(path=str(out/(name+'.png')))
   # Save a document-width image separately; no claim the detailed atlas is readable at 800px.
   page.evaluate('(w)=>{let s=document.querySelector("svg");s.style.width=w+"px";s.style.height="auto"}',rw)
   page.locator('svg').first.screenshot(path=str(out/(name+'-reading.png')))
   report.append({'diagram':name,'width':data['width'],'height':data['height'],'reading_width':rw,'minimum_type_at_reading_width':round(minimum,2),'text_lines':len(data['labels']),'errors':errors})
  version=browser.version;browser.close()
 r={'chromium':version,'method':'embedded fonts loaded; actual glyph bounds (Canvas ascent/descent, SVG positions); 3px centerline clearance','diagrams':report};(directory/'browser-checks.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(report,indent=2))
 return all(not r['errors'] for r in report)
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--directory',type=Path,default=Path(__file__).parent);ap.add_argument('--screenshots-dir',type=Path,required=True);ap.add_argument('--chromium',default='/usr/bin/chromium');ap.add_argument('--report-only',action='store_true');a=ap.parse_args()
 if not inspect(a.directory,a.screenshots_dir,a.chromium) and not a.report_only:raise SystemExit('Browser geometry qualification failed')
if __name__=='__main__':main()
