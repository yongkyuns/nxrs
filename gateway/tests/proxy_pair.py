"""Public approved-pair arguments and owned /run-only corruption fixtures.

The real Rust installer performs validation/publication. Callers retain their
hosted-runner or exact-guest admission, command deadlines and cleanup ownership.
"""
import hashlib,json,os,re,shutil,stat
from pathlib import Path

SCOPE='operator-approved-minimal-camera-proxy-selection'

def require(value,category):
    if not value:raise ValueError(category)

def read(path,maximum):
    with open(path,'rb',opener=lambda name,flags:os.open(name,flags|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        info=os.fstat(source.fileno());require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=maximum,'bounded-fixture-pair-file')
        raw=source.read(maximum+1);require(len(raw)<=maximum,'bounded-fixture-pair-read');return raw

def install_arguments(installer,review,destination,expected='none'):
    approval=read(review/'review.json',8192);proof=read(review/'qualification.json',1024*1024)
    digest=hashlib.sha256(approval).hexdigest();qhash=hashlib.sha256(proof).hexdigest()
    require(json.loads(approval)['qualification_sha256']==qhash,'same-fixture-pair-proof')
    require(expected=='none' or re.fullmatch('[a-f0-9]{64}',expected),'fixture-expected-selection')
    return [str(installer),'--review-dir',str(review),'--review-sha256',digest,
            '--qualification-sha256',qhash,'--destination',str(destination),'--expected-active',expected]

def selected_version(store):
    value=json.loads(read(store/'active.json',1024))
    require(set(value)=={'schema','scope','approval_sha256'} and type(value['schema']) is int and value['schema']==1
            and value['scope']==SCOPE and re.fullmatch('[a-f0-9]{64}',value['approval_sha256']),'fixture-selection-schema')
    version=store/'versions'/value['approval_sha256']
    require(hashlib.sha256(read(version/'approval.json',8192)).hexdigest()==value['approval_sha256'],'fixture-selected-policy')
    return version

def select_invalid_policy(store,template,raw):
    """Deliberate installed-policy corruption for named hosted refusal cases."""
    require(os.geteuid()==0 and store.name=='proxy-artifact' and store.parent.parent==Path('/run') and
            re.fullmatch('nxrs-[a-z]+-[a-f0-9]{8}',store.parent.name),'owned-run-policy-corruption-fixture')
    root=store.lstat();require(stat.S_ISDIR(root.st_mode) and root.st_uid==0 and root.st_mode&0o022==0,'owned-fixture-pair-root')
    digest=hashlib.sha256(raw).hexdigest();version=store/'versions'/digest;version.mkdir(mode=0o755)
    for name,mode in [('nginx',0o755),('qualification.json',0o600)]:
        shutil.copyfile(template/name,version/name);(version/name).chmod(mode)
    with open(version/'approval.json','xb') as output:output.write(raw)
    (version/'approval.json').chmod(0o644)
    temporary=store/'.fixture-selection.pending'
    with open(temporary,'x') as output:
        json.dump(dict(schema=1,scope=SCOPE,approval_sha256=digest),output,sort_keys=True);output.flush();os.fsync(output.fileno())
    temporary.chmod(0o644);os.replace(temporary,store/'active.json')
    directory=os.open(store,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return digest
