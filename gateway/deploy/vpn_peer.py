#!/usr/bin/env python3
"""Offline reference peer replacement/retirement/recovery. Never apply networking or reset history."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

MAX_RETIRED=4096
CONFIG='wg-nxrs.conf'
HISTORY='vpn-peers.json'
MARKER='rotation.json'


def require(condition, category):
    if not condition:raise ValueError(category)


def public_key(value):
    require(isinstance(value,str),'WireGuard key encoding required')
    try:raw=base64.b64decode(value,validate=True)
    except (ValueError,TypeError):raise ValueError('invalid WireGuard key encoding') from None
    require(len(raw)==32 and raw!=bytes(32) and base64.b64encode(raw).decode()==value,'invalid WireGuard key encoding')
    return value


def private_address(value):
    ip=ipaddress.IPv4Address(value)
    require(any(ip in ipaddress.ip_network(n) for n in ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']),
        'RFC1918 tunnel address required')
    return str(ip)


def configuration(gateway,peer,secret,peer_key):
    gateway=private_address(gateway);public_key(secret)
    require((peer is None)==(peer_key is None),'coherent active peer required')
    text=(f'[Interface]\nAddress = {gateway}/32\nListenPort = 51820\nMTU = 1380\n'
        'PreUp = /usr/sbin/nft -f /etc/nxrs-monitor/firewall.nft\n'
        f'PrivateKey = {secret}\n')
    if peer_key is not None:
        peer=private_address(peer);public_key(peer_key)
        require(gateway!=peer,'distinct tunnel addresses required')
        text+=f'\n[Peer]\nPublicKey = {peer_key}\nAllowedIPs = {peer}/32\n'
    return text


def parse_configuration(raw):
    require(isinstance(raw,bytes) and len(raw)<=8192,'bounded reference WireGuard configuration required')
    try:text=raw.decode('ascii')
    except UnicodeError:raise ValueError('reference WireGuard configuration required') from None
    def field(name):
        values=re.findall(r'^'+name+r' = ([^\n]+)$',text,re.M)
        require(len(values)==1,'unambiguous reference WireGuard field required')
        return values[0]
    gateway=field('Address');require(gateway.endswith('/32'),'single host routes required')
    peer=None;peer_key=None
    if '\n[Peer]\n' in text:
        peer=field('AllowedIPs');require(peer.endswith('/32'),'single host routes required')
        peer=peer[:-3];peer_key=field('PublicKey')
    result=dict(gateway=gateway[:-3],peer=peer,secret=field('PrivateKey'),peer_key=peer_key)
    require(configuration(**result)==text,'exact single-peer reference profile required')
    return result


def encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()


def digest(raw):return hashlib.sha256(raw).hexdigest()


def initial_history(config,gateway_public):
    selected=parse_configuration(config);public_key(gateway_public)
    require(selected['peer_key'] is not None,'new enrollment requires an active independent peer')
    require(gateway_public!=selected['peer_key'],'independent gateway and peer keys required')
    return dict(version=1,generation=1,gateway_public_key=gateway_public,active_peer=selected['peer_key'],
        retired_peers=[],config_sha256=digest(config))


def parse_history(raw):
    def unique(rows):
        value={}
        for key,item in rows:
            require(key not in value,'duplicate VPN history field');value[key]=item
        return value
    require(isinstance(raw,bytes) and len(raw)<=1024*1024,'bounded VPN history required')
    value=json.loads(raw,object_pairs_hook=unique)
    fields={'version','generation','gateway_public_key','active_peer','retired_peers','config_sha256'}
    require(isinstance(value,dict) and type(value.get('version')) is int and value['version'] in [1,2]
        and type(value.get('generation')) is int,'supported VPN history version required')
    version=value['version'];active=value.get('active_peer')
    require(set(value)==(fields if version==1 else fields|{'peer_address'}),'exact VPN history schema required')
    if version==1:public_key(active)
    elif active is not None:public_key(active)
    retired=value['retired_peers']
    # A full legacy ledger still has one active key. Reserve its final slot for
    # emergency retirement, then refuse any new admission rather than drop history.
    require(isinstance(retired,list) and len(retired)+(active is not None)<=MAX_RETIRED+1,'bounded VPN retirement history required')
    for key in [value['gateway_public_key'],*retired]:public_key(key)
    require(len(set(retired))==len(retired) and active not in retired
        and value['gateway_public_key'] not in [active,*retired],'coherent VPN retirement history required')
    if version==1:require(value['generation']==len(retired)+1,'coherent VPN retirement history required')
    else:
        require(private_address(value['peer_address'])==value['peer_address'],'canonical retained peer address required')
        require(len(retired)+1<=value['generation']<=2*len(retired)+(active is not None),
            'coherent VPN transition generation required')
    require(isinstance(value['config_sha256'],str) and re.fullmatch('[0-9a-f]{64}',value['config_sha256']),
        'VPN configuration digest required')
    return value


def private_read(path,limit):
    require(os.name=='posix','private Unix configuration files required')
    path=Path(path);require(path.is_absolute(),'absolute configuration path required')
    parent=path.parent.resolve(strict=True)
    directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        d=os.fstat(directory)
        require(stat.S_ISDIR(d.st_mode) and d.st_uid==os.geteuid() and d.st_mode&0o077==0,'private owned configuration directory required')
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid==os.geteuid()
                and before.st_mode&0o077==0 and before.st_nlink==1 and 0<before.st_size<=limit,
                'private owned regular configuration required')
            raw=source.read(limit+1)
            after=os.fstat(source.fileno());selected=os.stat(path.name,dir_fd=directory,follow_symlinks=False)
            identity=lambda m:(m.st_dev,m.st_ino,m.st_mode,m.st_uid,m.st_gid,m.st_nlink,m.st_size,m.st_mtime_ns,m.st_ctime_ns)
            require(len(raw)<=limit and identity(before)==identity(after)==identity(selected),'stable private configuration required')
            return raw
    finally:os.close(directory)


def pair(config,history):
    config_raw=private_read(config,8192);history_raw=private_read(history,1024*1024)
    selected=parse_configuration(config_raw);state=parse_history(history_raw)
    require(state['config_sha256']==digest(config_raw) and state['active_peer']==selected['peer_key'],'matching VPN configuration/history pair required')
    if state['version']==2:
        require(state['peer_address']!=selected['gateway']
            and (selected['peer'] is None or selected['peer']==state['peer_address']),'retained peer address must match configuration')
    return config_raw,history_raw,selected,state


def write_private(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as output:
        os.fchmod(output.fileno(),0o600);output.write(raw);output.flush();os.fsync(output.fileno())


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def transition(before,operation,expected_old=None,new_peer=None,expected_generation=None):
    _,_,selected,state=before
    active=state['active_peer'];retired=state['retired_peers']
    require(operation in ['replace','retire','recover'],'explicit VPN transition required')
    if operation in ['replace','retire']:
        public_key(expected_old);require(active==expected_old,'expected current VPN peer required')
    else:
        require(state['version']==2 and active is None,'recovery requires explicit retired state')
        require(type(expected_generation) is int and expected_generation==state['generation'],'expected retired generation required')
    if operation in ['replace','recover']:
        public_key(new_peer)
        require(new_peer not in [active,state['gateway_public_key'],*retired],'fresh independent VPN peer required')
        require(len(retired)<(MAX_RETIRED if operation=='replace' else MAX_RETIRED+1),'VPN retirement history capacity exceeded')
    else:require(new_peer is None,'retirement cannot introduce a peer')
    address=selected['peer'] if active is not None else state['peer_address']
    replacement=configuration(selected['gateway'],address if new_peer is not None else None,selected['secret'],new_peer).encode()
    following={**state,'generation':state['generation']+1,'active_peer':new_peer,
        'retired_peers':[*retired,active] if active is not None else list(retired),'config_sha256':digest(replacement)}
    if operation=='retire':following.update(version=2,peer_address=address)
    parse_history(encoded(following))
    return replacement,following


def marker(before,replacement,following,operation):
    raw,history,_,state=before
    legacy=state['version']==1 and operation=='replace'
    value=dict(version=1 if legacy else 2,scope='staged-reference-vpn-peer-rotation' if legacy else 'staged-reference-vpn-peer-transition',
        source_config_sha256=digest(raw),source_history_sha256=digest(history),
        staged_config_sha256=digest(replacement),staged_history_sha256=digest(encoded(following)),
        previous_generation=state['generation'],generation=following['generation'],
        physical_qualified=False,deployment_permitted=False)
    if not legacy:value['operation']=operation
    return value


def stage_transition(config,history,destination,operation,**selection):
    before=pair(config,history)
    replacement,following=transition(before,operation,**selection)
    destination=Path(destination)
    require(destination.is_absolute(),'absolute new private destination required')
    destination.mkdir(mode=0o700,parents=False,exist_ok=False);os.chmod(destination,0o700)
    try:
        write_private(destination/CONFIG,replacement);write_private(destination/HISTORY,encoded(following))
        sync_directory(destination)
        require(pair(config,history)==before,'rotation source changed during staging')
        write_private(destination/MARKER,encoded(marker(before,replacement,following,operation)));sync_directory(destination)
    except Exception:
        try:(destination/MARKER).unlink()
        except FileNotFoundError:pass
        raise
    return destination


def stage(config,history,destination,expected_old,new_peer):
    return stage_transition(config,history,destination,'replace',expected_old=expected_old,new_peer=new_peer)


def stage_retirement(config,history,destination,expected_old):
    return stage_transition(config,history,destination,'retire',expected_old=expected_old)


def stage_recovery(config,history,destination,expected_generation,new_peer):
    return stage_transition(config,history,destination,'recover',expected_generation=expected_generation,new_peer=new_peer)


def check(config,history,destination):
    before=pair(config,history);state=before[3]
    destination=Path(destination)
    staged=pair(destination/CONFIG,destination/HISTORY);next_raw,next_history,_,next_state=staged
    marker_raw=private_read(destination/MARKER,8192)
    active=state['active_peer'];following_active=next_state['active_peer']
    require(active is not None or following_active is not None,'an actual VPN transition is required')
    operation='retire' if following_active is None else ('recover' if active is None else 'replace')
    replacement,following=transition(before,operation,expected_old=active,new_peer=following_active,expected_generation=state['generation'])
    require(next_raw==replacement and next_history==encoded(following),'only a fresh peer transition with retained history is permitted')
    require(marker_raw==encoded(marker(before,replacement,following,operation)),'complete current VPN rotation marker required')
    require(pair(config,history)==before and pair(destination/CONFIG,destination/HISTORY)==staged
        and private_read(destination/MARKER,8192)==marker_raw,'rotation inputs changed during review')
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    modes={name:commands.add_parser(name) for name in ['stage','stage-retirement','stage-recovery','check']}
    for command in modes.values():
        command.add_argument('--config',type=Path,required=True)
        command.add_argument('--history',type=Path,required=True)
        command.add_argument('--destination',type=Path,required=True)
    for name in ['stage','stage-retirement']:modes[name].add_argument('--expected-peer',required=True)
    for name in ['stage','stage-recovery']:modes[name].add_argument('--replacement-peer',required=True)
    modes['stage-recovery'].add_argument('--expected-generation',type=int,required=True)
    args=parser.parse_args()
    try:
        if args.command=='stage':stage(args.config,args.history,args.destination,args.expected_peer,args.replacement_peer)
        elif args.command=='stage-retirement':stage_retirement(args.config,args.history,args.destination,args.expected_peer)
        elif args.command=='stage-recovery':stage_recovery(args.config,args.history,args.destination,args.expected_generation,args.replacement_peer)
        else:check(args.config,args.history,args.destination)
    except (ValueError,OSError,RecursionError):
        parser.exit(1,'VPN transition refused. Preserve existing configuration/history; never deploy partial or stale output.\n')
    print('Completed local VPN file review. No network, interface, routes or services were changed.')
