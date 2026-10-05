#!/usr/bin/env python3
"""Consistency gates; cannot prove architectural or upstream semantic correctness."""
from pathlib import Path
from urllib.parse import urlparse,unquote
from markdown_it import MarkdownIt
import json,re,hashlib,collections,sys
ROOT=Path.cwd();R=ROOT/'docs/references'
md=MarkdownIt('commonmark').enable('table')
expected=[
'1. Role and architectural position','2. Languages and runtime model','3. APIs, contracts and portability boundaries',
'4. Execution, scheduling and ISR boundaries','5. Memory, ownership and protection','6. Data flow and communication contracts',
'7. Hardware integration and measurement semantics','8. Configuration, startup and lifecycle','9. Timing, observability and qualification','10. Lessons for nxrs']
errors=[];total_local=total_external=0;allurls=set();anchors={}
def slug(s):
 s=re.sub(r'<[^>]*>','',s);s=re.sub(r'[^\w\s-]','',s.lower(),flags=re.UNICODE);return s.replace(' ','-')
def readanchors(p):
 if p in anchors:return anchors[p]
 toks=md.parse(p.read_text());counts=collections.Counter();result=set()
 for i,t in enumerate(toks):
  if t.type=='heading_open':
   s=''.join(x.content for x in toks[i+1].children or [] if x.type in ('text','code_inline'));s=slug(s);n=counts[s];counts[s]+=1;result.add(s+('-'+str(n) if n else ''))
 result.update(re.findall(r'<a\s+(?:name|id)="([^"]+)"',p.read_text()))
 anchors[p]=result;return result
for p in sorted(R.rglob('*.md')):
 text=p.read_text();env={};tokens=md.parse(text,env)
 defs=re.findall(r'^\[([^]]+)\]:\s*(.+)$',text,re.M);seen={}
 for name,url in defs:
  name=name.lower()
  if name in seen and seen[name]!=url:errors.append([str(p.relative_to(R)),'conflicting_reference',name])
  seen[name]=url
 for m in re.finditer(r'\[[^]\n]+\]\[([^]\n]+)\]',text):
  if m.group(1).lower() not in seen:errors.append([str(p.relative_to(R)),'undefined_reference',m.group(1)])
 links=[]
 for t in tokens:
  for c in t.children or []:
   if c.type=='link_open':links.append(c.attrGet('href'))
   if c.type=='image':links.append(c.attrGet('src'))
 for link in links:
  if not link:continue
  u=urlparse(link)
  if u.scheme or u.netloc:total_external+=1;allurls.add(link);continue
  total_local+=1;target=(p.parent/unquote(u.path)).resolve() if u.path else p.resolve()
  if not target.exists():errors.append([str(p.relative_to(R)),'missing_target',link]);continue
  if u.fragment and target.suffix=='.md' and unquote(u.fragment) not in readanchors(target):
   errors.append([str(p.relative_to(R)),'missing_anchor',link])
for system in ('openvela','zephyr','px4'):
 t=(R/system/'README.md').read_text();heads=re.findall(r'^## (.+)$',t,re.M)
 if heads!=expected:errors.append([system,'headings',heads])
 for f in ('README.md','architecture-atlas.md','sources.md','diagrams/README.md'):
  if not (R/system/f).exists():errors.append([system,'missing_role',f])
 if '5c0d6360ef5190346ddfd41aec766895800ba287' not in (R/system/'sources.md').read_text():errors.append([system,'inconsistent_baseline'])
 top='\n'.join(t.splitlines()[:8])
 if re.search(r'rollback|latest revision|expanded|iteration|[0-9a-f]{40}',top,re.I):errors.append([system,'history_in_front_matter'])
 ds=[p for p in (R/system/'diagrams').glob('*.d2') if p.name!='material.d2']
 want={'openvela':5,'zephyr':6,'px4':14}[system]
 if len(ds)!=want:errors.append([system,'lost_diagrams',len(ds)])
 for d in ds:
  if not d.with_suffix('.svg').exists():errors.append([system,'missing_svg',d.name])
  for imp in re.findall(r'@([\w./-]+\.d2)',d.read_text()):
   if not (d.parent/imp).exists():errors.append([system,'missing_import',imp])
report={'markdown_files':len(list(R.rglob('*.md'))),'common_topics_per_system':len(expected),'systems':3,'diagrams':25,'local_links_and_anchors_checked':total_local,'external_link_occurrences':total_external,'unique_external_urls':len(allurls),'errors':errors,'limits':'Local links and structural coverage only; source semantics reviewed separately. External URLs were not HTTP-checked by this script.'}
Path('.cohesion-staging/document-checks.json').write_text(json.dumps(report,indent=2)+'\n')
Path('.cohesion-staging/external-urls.txt').write_text('\n'.join(sorted(allurls))+'\n')
print(json.dumps(report,indent=2));sys.exit(bool(errors))
