#!/usr/bin/env python3
"""Offline single-peer reference rotation. Never apply networking or reset history."""
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
    gateway,peer=private_address(gateway),private_address(peer)
    require(gateway!=peer,'distinct tunnel addresses required')
    public_key(secret);public_key(peer_key)
    return (f'[Interface]\nAddress = {gateway}/32\nListenPort = 51820\nMTU = 1380\n'
        'PreUp = /usr/sbin/nft -f /etc/nxrs-monitor/firewall.nft\n'
        f'PrivateKey = {secret}\n\n[Peer]\nPublicKey = {peer_key}\nAllowedIPs = {peer}/32\n')


def parse_configuration(raw):
    require(isinstance(raw,bytes) and len(raw)<=8192,'bounded reference WireGuard configuration required')
    try:text=raw.decode('ascii')
    except UnicodeError:raise ValueError('reference WireGuard configuration required') from None
    def field(name):
        values=re.findall(r'^'+name+r' = ([^\n]+)$',text,re.M)
        require(len(values)==1,'unambiguous reference WireGuard field required')
        return values[0]
    gateway=field('Address');peer=field('AllowedIPs')
    require(gateway.endswith('/32') and peer.endswith('/32'),'single host routes required')
    result=dict(gateway=gateway[:-3],peer=peer[:-3],secret=field('PrivateKey'),peer_key=field('PublicKey'))
    require(configuration(**result)==text,'exact single-peer reference profile required')
    return result


def encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()


def digest(raw):return hashlib.sha256(raw).hexdigest()


def initial_history(config,gateway_public):
    selected=parse_configuration(config);public_key(gateway_public)
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
    require(isinstance(value,dict) and set(value)=={'version','generation','gateway_public_key','active_peer','retired_peers','config_sha256'},
        'exact VPN history schema required')
    require(type(value['version']) is int and value['version']==1 and type(value['generation']) is int,
        'supported VPN history version required')
    retired=value['retired_peers']
    require(isinstance(retired,list) and len(retired)<=MAX_RETIRED,'bounded VPN retirement history required')
    for key in [value['gateway_public_key'],value['active_peer'],*retired]:public_key(key)
    require(len(set(retired))==len(retired) and value['active_peer'] not in retired
        and value['gateway_public_key'] not in [value['active_peer'],*retired]
        and value['generation']==len(retired)+1,'coherent VPN retirement history required')
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
    return config_raw,history_raw,selected,state


def write_private(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as output:
        os.fchmod(output.fileno(),0o600);output.write(raw);output.flush();os.fsync(output.fileno())


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def stage(config,history,destination,expected_old,new_peer):
    public_key(expected_old);public_key(new_peer)
    before=pair(config,history);config_raw,history_raw,selected,state=before
    require(state['active_peer']==expected_old,'expected current VPN peer required')
    require(new_peer not in [expected_old,state['gateway_public_key'],*state['retired_peers']],'fresh independent VPN peer required')
    require(len(state['retired_peers'])<MAX_RETIRED,'VPN retirement history capacity exceeded')
    replacement=configuration(**{**selected,'peer_key':new_peer}).encode()
    following={**state,'generation':state['generation']+1,'active_peer':new_peer,
        'retired_peers':[*state['retired_peers'],expected_old],'config_sha256':digest(replacement)}
    parse_history(encoded(following))
    destination=Path(destination)
    require(destination.is_absolute(),'absolute new private destination required')
    destination.mkdir(mode=0o700,parents=False,exist_ok=False);os.chmod(destination,0o700)
    try:
        write_private(destination/CONFIG,replacement);write_private(destination/HISTORY,encoded(following))
        sync_directory(destination)
        require(pair(config,history)==before,'rotation source changed during staging')
        marker=dict(version=1,scope='staged-reference-vpn-peer-rotation',
            source_config_sha256=digest(config_raw),source_history_sha256=digest(history_raw),
            staged_config_sha256=digest(replacement),staged_history_sha256=digest(encoded(following)),
            previous_generation=state['generation'],generation=following['generation'],
            physical_qualified=False,deployment_permitted=False)
        write_private(destination/MARKER,encoded(marker));sync_directory(destination)
    except Exception:
        try:(destination/MARKER).unlink()
        except FileNotFoundError:pass
        raise
    return destination


def check(config,history,destination):
    before=pair(config,history);raw,old_history,selected,state=before
    destination=Path(destination)
    staged=pair(destination/CONFIG,destination/HISTORY);next_raw,next_history,following,next_state=staged
    marker_raw=private_read(destination/MARKER,8192)
    # Compare the whole expected marker, including host/deployment scope.
    expected=dict(version=1,scope='staged-reference-vpn-peer-rotation',
        source_config_sha256=digest(raw),source_history_sha256=digest(old_history),
        staged_config_sha256=digest(next_raw),staged_history_sha256=digest(next_history),
        previous_generation=state['generation'],generation=state['generation']+1,
        physical_qualified=False,deployment_permitted=False)
    require(marker_raw==encoded(expected),'complete current VPN rotation marker required')
    require(following=={**selected,'peer_key':next_state['active_peer']}
        and next_state=={**state,'generation':state['generation']+1,'active_peer':next_state['active_peer'],
            'retired_peers':[*state['retired_peers'],state['active_peer']],'config_sha256':digest(next_raw)},
        'only a fresh peer replacement with retained history is permitted')
    require(next_state['active_peer'] not in [state['active_peer'],state['gateway_public_key'],*state['retired_peers']],
        'retired or reused VPN peer refused')
    require(pair(config,history)==before and pair(destination/CONFIG,destination/HISTORY)==staged
        and private_read(destination/MARKER,8192)==marker_raw,'rotation inputs changed during review')
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    render=commands.add_parser('stage')
    review=commands.add_parser('check')
    for command in [render,review]:
        command.add_argument('--config',type=Path,required=True)
        command.add_argument('--history',type=Path,required=True)
        command.add_argument('--destination',type=Path,required=True)
    render.add_argument('--expected-peer',required=True);render.add_argument('--replacement-peer',required=True)
    args=parser.parse_args()
    try:
        if args.command=='stage':stage(args.config,args.history,args.destination,args.expected_peer,args.replacement_peer)
        else:check(args.config,args.history,args.destination)
    except (ValueError,OSError):
        parser.exit(1,'VPN rotation refused. Preserve existing configuration/history; never deploy partial or stale output.\n')
    print('Completed local VPN file review. No network, interface, routes or services were changed.')
