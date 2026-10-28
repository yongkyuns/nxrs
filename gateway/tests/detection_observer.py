"""Bounded authenticated API observations for the named SDK detection fixture.

This is a host qualification observer, not a client renderer or an image mapping
proof. Each observation owns and closes one finite response; no background task
or unbounded event history is created.
"""
import asyncio
import hashlib
import json
import re

LIMIT=8192
SCOPE='actual-sdk-tls-gateway-detection-api'


def require(value):
    if not value:raise AssertionError('bounded SDK detection API observation required')


def integer(value,minimum=0,maximum=2**63-1):
    return type(value) is int and minimum<=value<=maximum


def unique(rows):
    result={}
    for key,value in rows:
        require(key not in result);result[key]=value
    return result


def snapshot(raw,camera,event_id):
    require(type(raw) is bytes and 0<len(raw)<=LIMIT)
    value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:require(False))
    require(type(value) is dict and set(value)=={'api_version','camera_id','source_session_id','revision',
        'source_online','presence','stream_elapsed_ms','observation_age_ms','valid_for_ms',
        'frame_synchronized','source','event_gap'})
    require(value['api_version']=='1' and value['camera_id']==camera and value['frame_synchronized'] is False)
    require(all(type(value[k]) is bool for k in ['source_online','event_gap']))
    require(all(integer(value[k]) for k in ['revision','stream_elapsed_ms']) and integer(value['valid_for_ms'],0,1500))
    require(value['presence'] in ('unavailable','clear','person'))
    require(value['observation_age_ms'] is None or integer(value['observation_age_ms']))
    source=value['source'];session=value['source_session_id']
    if source is None:
        require(value['presence']=='unavailable' and (session is None or type(session) is str
            and re.fullmatch('[0-9a-f]{32}',session) is not None) and event_id==(session or 'none')+':0')
        return value
    require(type(session) is str and re.fullmatch('[0-9a-f]{32}',session) is not None)
    require(type(source) is dict and set(source)=={'api_version','publication_id','emitted_ms','presence',
        'localization','latest','event_count','events'})
    require(source['api_version']=='1' and source['localization'] is False
        and integer(source['publication_id'],1) and integer(source['emitted_ms']) and integer(source['event_count'])
        and source['presence'] in ('unavailable','clear','person'))
    require(event_id==session+':'+str(source['event_count']))
    require(type(source['events']) is list and len(source['events'])<=8)
    previous=0
    for event in source['events']:
        require(type(event) is dict and set(event)=={'id','at_ms','kind'}
            and integer(event['id'],previous+1,source['event_count']) and integer(event['at_ms'],0,source['emitted_ms'])
            and event['kind'] in ('person_entered','person_left','unavailable'))
        previous=event['id']
    require(previous==source['event_count'])
    latest=source['latest']
    if latest is not None:
        require(type(latest) is dict and set(latest)=={'frame_sequence','captured_ms','inference_us',
            'person_score_permille','objects','truncated'})
        require(integer(latest['frame_sequence'],1) and integer(latest['captured_ms'],0,source['emitted_ms'])
            and integer(latest['inference_us']) and integer(latest['person_score_permille'],0,1000)
            and latest['objects']==[] and latest['truncated'] is False)
    else:require(source['presence']=='unavailable')
    if value['presence']=='person':
        require(value['source_online'] and source['presence']=='person' and latest is not None
            and latest['person_score_permille']==900 and integer(value['observation_age_ms'],0,4999)
            and integer(value['valid_for_ms'],1,1500))
    return value


async def observe(http,url,token,camera,presence,*,different_from=None):
    require(presence in ('person','unavailable'))
    fields={};size=0;total=0;messages=0;publications=set();selected_session=None;last_publication=0;last_sequence=0
    async with asyncio.timeout(6):
        async with http.get(url,headers={'Authorization':'Bearer '+token},timeout=None,read_bufsize=4096) as response:
            require(response.status==200 and response.headers.get('Content-Type','').split(';')[0]=='text/event-stream')
            while messages<20:
                line=await response.content.readline();require(type(line) is bytes and bool(line))
                total+=len(line);size+=len(line);require(size<=LIMIT and total<=64*1024)
                require(len(line)<=LIMIT and line.endswith(b'\n'))
                line=line.rstrip(b'\r\n')
                if line.startswith(b':'):continue
                if not line:
                    if not fields:size=0;continue
                    require(set(fields)=={b'event',b'id',b'data'} and fields[b'event']==b'detection')
                    value=snapshot(fields[b'data'],camera,fields[b'id'].decode('ascii'));messages+=1
                    fields={};size=0
                    if value['presence']!=presence:continue
                    if presence=='unavailable':
                        return dict(presence=presence,messages=messages,publications=0,session=None,response_closed=True)
                    session=value['source_session_id']
                    if different_from is not None and session==different_from:continue
                    if selected_session!=session:publications.clear();selected_session=session;last_publication=0;last_sequence=0
                    current=value['source']['publication_id'];sequence=value['source']['latest']['frame_sequence']
                    require(current>=last_publication and sequence>=last_sequence)
                    if current==last_publication:require(sequence==last_sequence)
                    if current>last_publication and sequence>last_sequence:publications.add(current)
                    last_publication=current;last_sequence=sequence;require(len(publications)<=20)
                    if len(publications)>=2:
                        return dict(presence=presence,messages=messages,publications=len(publications),session=session,response_closed=True)
                    continue
                key,separator,data=line.partition(b':');require(separator and key in (b'event',b'id',b'data') and key not in fields)
                fields[key]=data.removeprefix(b' ')
    raise AssertionError('SDK detection API progress required')


def evidence(confirmed,unavailable):
    require(type(confirmed) is list and len(confirmed)==3 and type(unavailable) is list and len(unavailable)==2)
    for row in confirmed:
        require(row['presence']=='person' and integer(row['publications'],2,20) and row['response_closed'] is True
            and type(row['session']) is str and re.fullmatch('[0-9a-f]{32}',row['session']) is not None)
    require(len({r['session'] for r in confirmed})==3)
    require(all(r['presence']=='unavailable' and r['response_closed'] is True for r in unavailable))
    return dict(schema=1,scope=SCOPE,qualified=True,confirmed_publications=sum(r['publications'] for r in confirmed),
        source_session_sha256=[hashlib.sha256(r['session'].encode()).hexdigest() for r in confirmed],
        signed_update_new_source=True,stall_new_source=True,source_stall_expired=True,peer_revocation_expired=True,
        response_owners_joined=True,frame_synchronized=False,media_frame_alignment_qualified=False,
        physical_qualified=False,deployment_permitted=False)


def validate(value):
    expected={'schema','scope','qualified','confirmed_publications','source_session_sha256','signed_update_new_source',
        'stall_new_source','source_stall_expired','peer_revocation_expired','response_owners_joined','frame_synchronized',
        'media_frame_alignment_qualified','physical_qualified','deployment_permitted'}
    require(type(value) is dict and set(value)==expected and type(value['schema']) is int and value['schema']==1
        and value['scope']==SCOPE and value['qualified'] is True and integer(value['confirmed_publications'],6,60))
    hashes=value['source_session_sha256'];require(type(hashes) is list and len(hashes)==3
        and all(type(h) is str and re.fullmatch('[0-9a-f]{64}',h) for h in hashes) and len(set(hashes))==3)
    require(all(value[k] is True for k in ['signed_update_new_source','stall_new_source','source_stall_expired',
        'peer_revocation_expired','response_owners_joined']))
    require(all(value[k] is False for k in ['frame_synchronized','media_frame_alignment_qualified','physical_qualified','deployment_permitted']))
