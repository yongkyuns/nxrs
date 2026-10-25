#!/usr/bin/env python3
"""Review the complete current nginx and PCRE2 publisher advisory feeds."""
from datetime import datetime,timezone
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import ubuntu as native
NGINX='https://nginx.org/en/security_advisories.html'
PCRE='https://api.github.com/repos/PCRE2Project/pcre2/security-advisories?per_page=100'
POLICY='security/gateway-proxy-dependencies.json'


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(value).hexdigest()
def require(value,category):native.require(value,category)


class Text(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.values=[]
    def handle_data(self,data):self.values.append(data)
    def handle_starttag(self,tag,attrs):
        if tag=='br':self.values.append('\n')
    def text(self):return ''.join(self.values)


def version(value):
    require(isinstance(value,str) and re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?',value),'proxy-version')
    parts=tuple(map(int,value.split('.')));return parts if len(parts)==3 else parts+(0,)


def nginx_articles(raw):
    require(0<len(raw)<=512*1024,'nginx-advisory-response-budget')
    html=raw.decode('utf-8');blocks=re.findall(r'<li><p>(.*?)</p></li>',html,re.S);result={}
    require(1<=len(blocks)<=256,'complete-nginx-advisory-blocks')
    for block in blocks:
        parser=Text();parser.feed(block);text=parser.text();lines=[v.strip() for v in text.splitlines() if v.strip()]
        ids=re.findall(r'\bCVE-[0-9]{4}-[0-9]{4,}\b',text)
        if not ids:ids=re.findall(r'\bCORE-[0-9]{4}-[0-9]{4,}\b',text)
        require(len(ids)==1 and ids[0] not in result,'unique-nginx-advisory')
        safe=[line.removeprefix('Not vulnerable: ') for line in lines if line.startswith('Not vulnerable: ')]
        affected=[line.removeprefix('Vulnerable: ') for line in lines if line.startswith('Vulnerable: ')]
        require(len(safe)==len(affected)==1,'complete-nginx-version-fields')
        severity=re.findall(r'Severity:\s*(major|medium|minor|low|none)\b',text)
        require(len(severity)==1,'nginx-severity')
        result[ids[0]]=dict(text_sha256=digest(' '.join(text.split()).encode()),fixed=safe[0],affected=affected[0],severity=severity[0])
    all_cves=set(re.findall(r'\bCVE-[0-9]{4}-[0-9]{4,}\b',html))
    require(all_cves=={n for n in result if n.startswith('CVE-')},'every-published-nginx-cve')
    return result


def nginx_affected(row,selected):
    value=version(selected);text=row['affected'];fixed=row['fixed']
    if text.startswith('nginx/Windows '):
        # Qualification admits only an actual Linux build, not a Windows provider.
        text=text.removeprefix('nginx/Windows ');windows=True
    else:windows=False
    intervals=[]
    if text!='all':
        for token in text.split(', '):
            pieces=token.split('-');require(1<=len(pieces)<=2,'nginx-version-range')
            low=version(pieces[0]);high=version(pieces[-1]);require(low<=high,'ordered-nginx-range');intervals.append((low,high))
    fixes=[]
    if fixed!='none':
        for token in fixed.split(', '):
            require(token.endswith('+'),'nginx-fixed-branch');fixes.append(version(token[:-1]))
    if windows:return False
    affected=text=='all' or any(low<=value<=high for low,high in intervals)
    # Backports on an explicitly fixed stable branch override broad upstream ranges.
    patched=any(value[:2]==fix[:2] and value>=fix for fix in fixes)
    return affected and not patched


def pcre_articles(raw):
    require(0<len(raw)<=1024*1024,'pcre-advisory-response-budget');rows=native.parse(raw)
    require(isinstance(rows,list) and 1<=len(rows)<100,'complete-pcre-advisory-page');result={}
    for row in rows:
        identity=row['ghsa_id'];require(re.fullmatch(r'GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}',identity) and identity not in result,'unique-pcre-advisory')
        require(row['withdrawn_at'] is None,'pcre-withdrawal-review-required')
        entries=row['vulnerabilities'];require(isinstance(entries,list) and len(entries)==1,'complete-pcre-package-advisory')
        item=entries[0];require(item['package']==dict(ecosystem='',name='PCRE2'),'pcre-source-identity')
        # Upstream supplies a single first fixed release. An absent/ambiguous fix
        # is a refusal, including when a broad affected range has no upper endpoint.
        fixed=item['patched_versions'];version(fixed)
        core={key:row[key] for key in ['ghsa_id','cve_id','summary','description','vulnerabilities','severity','updated_at','withdrawn_at']}
        result[identity]=dict(record_sha256=digest(encoded(core)),fixed=fixed,affected=item['vulnerable_version_range'])
    return result


def review(policy,nginx,pcre,now=None):
    now=now or datetime.now(timezone.utc)
    require(type(policy['schema']) is int and policy['schema']==1 and policy['scope']=='hosted-minimal-native-camera-proxy','proxy-review-scope')
    review=policy['advisory_review'];begin=datetime.fromisoformat(review['reviewed_at']);end=datetime.fromisoformat(review['valid_until'])
    require(begin.tzinfo is not None and end.tzinfo is not None and begin<=now<end and 0<(end-begin).total_seconds()<=31*86400,'proxy-review-expired')
    require(nginx==review['nginx'] and pcre==review['pcre2'],'complete-current-proxy-advisory-review')
    nv=policy['sources']['nginx']['version'];pv=policy['sources']['pcre2']['version'];version(nv);version(pv)
    active=[n for n,row in nginx.items() if nginx_affected(row,nv)]
    require(all(version(pv)>=version(row['fixed']) for row in pcre.values()),'pcre-upstream-fix-required')
    # Preserve the universal legacy logging entry explicitly. Runtime qualification
    # must prove the generated log-discard policy; this is not a version fix/ignore.
    require(active==['CVE-2009-4487'] and nginx[active[0]]['severity']=='none','nginx-upstream-fix-required')
    return dict(nginx_articles=len(nginx),pcre2_articles=len(pcre),nginx_unfixed=active,pcre2_unfixed=[],
                nginx_feed_sha256=digest(encoded(nginx)),pcre2_feed_sha256=digest(encoded(pcre)),universal_log_behavior_requires_runtime_control=True)
