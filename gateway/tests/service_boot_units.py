"""Named virtual-boot fixture around the actual encrypted production unit group.

Only network/unlock and synthetic-source providers are fixture operations. The
application sandbox, encrypted mount, dependency and retirement edges come from
the same generators exercised by the hosted service-group qualification.
"""
from pathlib import Path
import hashlib,json,re
import encrypted_service_reference as service
import service_boot_xml as xml

PREFIX='nxrs-es-00000000'
ROOT=Path('/run')/PREFIX
MAPPING='es0000000000000000'
MOUNT=service.mount_name(PREFIX)
TARGET=PREFIX+'-monitor.target'
PROVIDER=PREFIX+'-network.service'
CHECKER='service-boot-check.service'
BASE=Path('/var/lib/service-boot')
CODE=Path('/usr/src/nxrs/gateway/tests/service_boot_guest.py')
CASES=('provisioned','rebooted','locked','recovered')
PUBLIC_FAILURE_SOURCES=frozenset(('gateway/tests/service_boot_guest.py','gateway/tests/service_boot_proxy.py',
    'tools/gateway-proxy/boundary.py','tools/gateway-proxy/advisories.py','tools/native-packages/ubuntu.py',
    'gateway/tests/failure_location.py','gateway/tests/test_vpn_deployment.py','gateway/deploy/vpn.py'))

def public_failure_locations(value):
    import failure_location
    require(isinstance(value,list) and len(value)<=8 and
            failure_location.project_locations(value,PUBLIC_FAILURE_SOURCES)==value,'fixed-public-boot-locations')
    require(len({(row['source'],row['line'],row['error']) for row in value})==len(value),'unique-public-boot-locations')
    return value

def require(value,category):service.require(value,category)

ACTIVATION_ROLES=('provider','target','mount','control','media','proxy','activity')
ACTIVATION_RESULTS=frozenset(('none','success','exit-code','signal','core-dump','watchdog','timeout',
    'exec-condition','oom-kill','resources','protocol','start-limit-hit','other'))

JOB_ROLES=frozenset((*ACTIVATION_ROLES,'device','stopper'))
JOB_OPERATIONS=frozenset(('start','stop','reload','restart','other'))
JOB_RESULTS=frozenset(('done','canceled','timeout','failed','dependency','skipped','invalid','assert',
    'unsupported','collected','once','frozen','other'))

LOAD_STATES=frozenset(('loaded','stub','not-found','bad-setting','error','merged','masked','other'))
ACTIVE_STATES=frozenset(('active','reloading','inactive','failed','activating','deactivating','maintenance','refreshing','other'))
PREREQUISITE_SUBSTATES=frozenset(('dead','tentative','plugged','mounting','mounting-done','mounted',
    'remounting','unmounting','failed','cleaning','other'))
TIMEOUT_KINDS=frozenset(('ninety-seconds','infinity','zero','other'))

def public_prerequisites(value):
    require(isinstance(value,dict) and set(value)=={'mapping_present','units','device_job_timeout','device_running_timeout'}
        and type(value['mapping_present']) is bool,'fixed-public-prerequisite-fields')
    require(all(type(value[key]) is str and value[key] in TIMEOUT_KINDS
        for key in ('device_job_timeout','device_running_timeout')),
        'fixed-public-device-timeouts')
    rows=value['units'];require(isinstance(rows,list) and len(rows)==2,'complete-public-prerequisites')
    for role,row in zip(('device','mount'),rows):
        require(isinstance(row,dict) and set(row)=={'role','load','state','substate','job_pending','result'}
            and row['role']==role and type(row['job_pending']) is bool,'fixed-public-prerequisite-unit')
        require(all(type(row[key]) is str and row[key] in known for key,known in
            (('load',LOAD_STATES),('state',ACTIVE_STATES),('substate',PREREQUISITE_SUBSTATES))),
            'fixed-public-prerequisite-states')
        require(row['result'] is None if role=='device' else type(row['result']) is str and row['result'] in ACTIVATION_RESULTS,
            'fixed-public-mount-result')
    return value

def public_job_results(value):
    require(isinstance(value,list) and len(value)<=16,'bounded-public-job-results')
    for item in value:
        require(isinstance(item,dict) and set(item)=={'role','operation','result'} and
            isinstance(item['role'],str) and item['role'] in JOB_ROLES and
            isinstance(item['operation'],str) and item['operation'] in JOB_OPERATIONS and
            isinstance(item['result'],str) and item['result'] in JOB_RESULTS,'fixed-public-job-result')
    require(len({(row['role'],row['operation'],row['result']) for row in value})==len(value),'distinct-public-job-results')
    return value

def public_activation(value):
    require(isinstance(value,list) and len(value)==len(ACTIVATION_ROLES),'complete-public-activation')
    for role,item in zip(ACTIVATION_ROLES,value):
        require(isinstance(item,dict) and set(item)=={'role','active','result','exit_status'} and
            item['role']==role and type(item['active']) is bool,'fixed-public-activation-fields')
        if role in ('target','mount'):
            require(item['result'] is None and item['exit_status'] is None,'non-service-activation')
        else:
            require(isinstance(item['result'],str) and item['result'] in ACTIVATION_RESULTS and
                type(item['exit_status']) is int and 0<=item['exit_status']<=255,'public-service-activation')
    return value


def command_case(text):
    require(isinstance(text,str) and len(text)<=4096,'bounded-service-boot-command-line')
    cases=re.findall(r'(?:^| )service_case=([^ ]+)(?= |$)',text)
    nonces=re.findall(r'(?:^| )service_nonce=([^ ]+)(?= |$)',text)
    require(len(cases)==len(nonces)==1 and cases[0] in CASES and
            re.fullmatch('[a-f0-9]{32}',nonces[0]),'explicit-service-boot-case')
    return cases[0],nonces[0]

def units():
    rendered=service.units(PREFIX,ROOT,MAPPING,MOUNT)
    # A mount has default Before=local-fs.target. Its named fixture provider
    # cannot wait for basic.target/local-fs.target without forming a boot cycle.
    rendered[PROVIDER]=(
        '[Unit]\nDescription=Owned virtual guest network and test-key provider\n'
        'DefaultDependencies=no\nAfter=systemd-remount-fs.service systemd-udev-trigger.service\n'
        'Before=local-fs.target\nConflicts=shutdown.target\nBefore=shutdown.target\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        'ExecStart=/usr/bin/python3 -B '+str(CODE)+' prepare\n'
        'TimeoutStartSec=240\nKillMode=control-group\nStandardOutput=null\nStandardError=null\n')
    rendered[CHECKER]=(
        '[Unit]\nDescription=Owned four-boot encrypted gateway witness\n'
        'Wants='+TARGET+'\nAfter=local-fs.target '+TARGET+'\n'
        '[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -B '+str(CODE)+' check\n'
        'TimeoutStartSec=300\nKillMode=control-group\nStandardOutput=null\nStandardError=null\n'
        '[Install]\nWantedBy=multi-user.target\n')
    # Source publication is deliberately admitted by the checker after HTTPS
    # and management readiness, never by Type=simple activation alone.
    source=PREFIX+'-source.service'
    rendered[source]=rendered[source].replace('ExecStart=/usr/bin/python3 ',
                                             'ExecStart=/usr/bin/python3 -B ',1)
    rendered[source]=rendered[source].replace(str(ROOT/'bin/source.py'),str(ROOT/'source.py'),1)
    return rendered

def unit_hashes():
    return {name:hashlib.sha256(raw.encode()).hexdigest() for name,raw in sorted(units().items())}

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def validate_plan(plan):
    fields={'schema','scope','nonce','head','kernel_release','pid1_sha256','source_manifest_sha256','inputs','files',
            'parent_proxy_qualification_sha256','parent_proxy_review_sha256','proxy_inputs','proxy_libraries','unit_sha256','runtime_links','system_xml_runtime'}
    require(set(plan)==fields and type(plan['schema']) is int and plan['schema']==2 and
            plan['scope']=='owned-generated-service-boot-plan' and re.fullmatch('[a-f0-9]{32}',plan['nonce']) and
            re.fullmatch('[a-f0-9]{40}',plan['head']) and
            re.fullmatch('[A-Za-z0-9_.+-]{1,100}',plan['kernel_release']),'exact-service-boot-plan')
    for key in ('pid1_sha256','source_manifest_sha256','parent_proxy_qualification_sha256','parent_proxy_review_sha256'):
        require(isinstance(plan[key],str) and re.fullmatch('[a-f0-9]{64}',plan[key]),'service-plan-proof-digest')
    expected_inputs=service.source_inputs()
    import candidate # source_inputs() above has loaded this fixture's source module.
    require(plan['inputs']==expected_inputs and plan['unit_sha256']==unit_hashes() and
            plan['proxy_inputs']==candidate.source_inputs(),'service-plan-source-and-unit-binding')
    require(set(plan['proxy_libraries'])=={'loader','libc','crypto','ssl'} and
            all(re.fullmatch('[a-f0-9]{64}',value) for value in plan['proxy_libraries'].values()),'service-plan-guest-abi')
    files=plan['files'];links=plan['runtime_links'];library='/usr/lib/nxrs-gateway-media-qualification/lib/'
    required={'/var/lib/service-boot/bin/'+name for name in ('control','media','proxy','activity','sandbox','publisher','reader','ffmpeg','nginx','installer','xml-probe')}
    required|={'/var/lib/service-boot/'+name for name in ('proxy-parent.json','proxy-review.json','source-manifest.json')}
    required|={str(xml.LIBRARY),str(xml.QUALIFICATION)}
    require(isinstance(files,dict) and required<=set(files) and 1<=len(files)<=512 and
            all(re.fullmatch('[a-f0-9]{64}',value) for value in files.values()),'complete-service-plan-images')
    require(files['/var/lib/service-boot/proxy-parent.json']==plan['parent_proxy_qualification_sha256'] and
            files['/var/lib/service-boot/proxy-review.json']==plan['parent_proxy_review_sha256'] and
            files['/var/lib/service-boot/source-manifest.json']==plan['source_manifest_sha256'],'service-plan-proof-file-joins')
    require(all(name in required or (name.startswith(library) and re.fullmatch('[A-Za-z0-9_.+-]+',name[len(library):]))
                for name in files),'owned-service-plan-images')
    require(isinstance(links,dict) and 1<=len(links)<=128 and not set(links)&set(files),'bounded-service-media-links')
    for name,target in links.items():
        require(name.startswith(library) and re.fullmatch('[A-Za-z0-9_.+-]+',name[len(library):]) and
                re.fullmatch('[A-Za-z0-9_.+-]+',target),'relative-service-media-link')
        seen=set();current=name
        while current in links:
            require(current not in seen,'no-service-media-link-cycle');seen.add(current);current=library+links[current]
        require(current in files,'declared-service-media-link-target')
    xml.validate_plan(plan)
    return plan
