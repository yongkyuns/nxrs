"""Bounded host/guest wire contract for the isolated package-boot experiment."""
import hashlib,json,re

SCOPE='virtual-ubuntu-package-cold-boot'
CASES=('installed','installed-again','unconfigured','vendor','changed')
NEGATIVE=set(CASES[2:])
STAGES=('context','ledger','admission','consumer','volume','prepare','shutdown')
COMMANDS=frozenset(('package-state loop-module dm-crypt-module format-volume open-volume format-encrypted-fs '
    'mount-encrypted-fs unmount-written-fs close-written-volume wrong-volume-key reopen-volume mount-restored-fs '
    'unmount-restored-fs join-restored-volume cleanup-owned-mount cleanup-owned-mapping packaged-interpreter-jit '
    'actual-consumer-linkage actual-packaged-command refused-start refused-unit-state recover-configure '
    'recover-candidate reset-recovered-unit run-consumer prepare-unconfigured prepare-vendor').split())
FIELDS={'schema','scope','case','nonce','plan_sha256','sequence','boot_id_sha256','machine_id_sha256',
        'previous_boot_id_sha256','kernel_release','pid1_sha256','network_devices','admission_refused',
        'refusal_result','consumer_passed','encrypted_payload_sha256','mapped_device_joined',
        'prepared_next_state','ledger_committed'}
NEXT=('installed','unconfigured','vendor','changed','installed')

def require(value,category):
    if not value:raise ValueError(category)

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def parse(raw):
    def unique(rows):
        result={}
        for key,value in rows:
            require(key not in result,'duplicate-boot-field');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)

PLAN_FIELDS={'nonce','kernel_release','pid1_sha256','guest_sha256','runner_sha256','contract_sha256',
             'package_version','package_sha256','library_name','library_sha256','changed_library_sha256',
             'vendor_version','vendor_archive_sha256'}

def validate_plan(plan):
    require(set(plan)==PLAN_FIELDS and re.fullmatch('[a-f0-9]{32}',plan['nonce']),'exact-boot-plan-schema')
    require(re.fullmatch('[0-9][a-z0-9.+-]{1,95}',plan['kernel_release']) and
            re.fullmatch(r'libpcre2-8\.so\.0\.[0-9]+\.[0-9]+',plan['library_name']) and
            plan['package_version']=='10.49-0nxrs1' and re.fullmatch('[0-9][A-Za-z0-9.+:~_-]{0,199}',plan['vendor_version']),
            'public-boot-plan-identities')
    for name in PLAN_FIELDS-{'nonce','kernel_release','library_name','package_version','vendor_version'}:
        require(isinstance(plan[name],str) and re.fullmatch('[a-f0-9]{64}',plan[name]),'boot-plan-digests')
    return plan

def command_case(text):
    cases=re.findall(r'(?:^| )qual_case=([^ ]+)',text)
    nonces=re.findall(r'(?:^| )qual_nonce=([^ ]+)',text)
    require(len(cases)==len(nonces)==1 and cases[0] in CASES and re.fullmatch('[a-f0-9]{32}',nonces[0]),'explicit-owned-guest-case')
    return cases[0],nonces[0]

def payload(nonce,case):return ('owned virtual encrypted package fixture:'+nonce+':'+case).encode()

def validate(value,plan,case):
    require(set(value)==FIELDS and type(value['schema']) is int and value['schema']==1 and value['scope']==SCOPE,'public-boot-report-schema')
    require(value['case']==case and value['nonce']==plan['nonce'] and value['plan_sha256']==sha(encoded(plan)+b'\n'),'fresh-boot-plan-binding')
    index=CASES.index(case)
    require(type(value['sequence']) is int and value['sequence']==index+1 and value['kernel_release']==plan['kernel_release']
            and value['pid1_sha256']==plan['pid1_sha256'] and value['network_devices']==['lo'],'actual-guest-system-context')
    for key in ['boot_id_sha256','machine_id_sha256','encrypted_payload_sha256']:
        require(isinstance(value[key],str) and re.fullmatch('[a-f0-9]{64}',value[key]),'boot-report-digest')
    prior=value['previous_boot_id_sha256']
    require(prior is None if index==0 else isinstance(prior,str) and re.fullmatch('[a-f0-9]{64}',prior),'prior-boot-ledger-binding')
    require(value['admission_refused'] is (case in NEGATIVE) and value['refusal_result']==('exit-code' if case in NEGATIVE else None),
            'actual-startup-admission-result')
    require(value['consumer_passed'] is True and value['mapped_device_joined'] is True and value['ledger_committed'] is True,'completed-guest-controls')
    require(value['encrypted_payload_sha256']==sha(payload(plan['nonce'],case)) and value['prepared_next_state']==NEXT[index],'encrypted-data-and-next-state')
    return value

def aggregate(records,plan):
    require(len(records)==len(CASES),'five-complete-virtual-boots')
    for case,value in zip(CASES,records):validate(value,plan,case)
    require(len({v['boot_id_sha256'] for v in records})==5 and len({v['machine_id_sha256'] for v in records})==1,'fresh-kernels-persistent-identity')
    require(all(records[i]['previous_boot_id_sha256']==records[i-1]['boot_id_sha256'] for i in range(1,5)),'durable-boot-ledger-chain')
