"""Synthetic common file-contract corpus for Python staging and Rust admission."""
import argparse
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import vpn_peer


def corpus():
    key=lambda byte:base64.b64encode(bytes([byte])*32).decode()
    values=[]
    def case(name,config,history,valid):
        values.append(dict(name=name,config=config.decode(),history=history.decode(),valid=valid))
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary).resolve();source=root/'source';source.mkdir(mode=0o700)
        raw=vpn_peer.configuration('10.77.0.1','10.77.0.2',key(9),key(1)).encode()
        state=vpn_peer.initial_history(raw,key(8))
        vpn_peer.write_private(source/vpn_peer.CONFIG,raw);vpn_peer.write_private(source/vpn_peer.HISTORY,vpn_peer.encoded(state))
        rotated=vpn_peer.stage(source/vpn_peer.CONFIG,source/vpn_peer.HISTORY,root/'rotated',key(1),key(2))
        retired=vpn_peer.stage_retirement(rotated/vpn_peer.CONFIG,rotated/vpn_peer.HISTORY,root/'retired',key(2))
        recovered=vpn_peer.stage_recovery(retired/vpn_peer.CONFIG,retired/vpn_peer.HISTORY,root/'recovered',3,key(3))
        for name,directory in [('enrolled-v1',source),('replaced-v1',rotated),('retired-v2',retired),('recovered-v2',recovered)]:
            case(name,(directory/vpn_peer.CONFIG).read_bytes(),(directory/vpn_peer.HISTORY).read_bytes(),True)
        for field in state:
            h=copy.deepcopy(state);h.pop(field);case('missing-'+field,raw,vpn_peer.encoded(h),False)
        for field,value in [('active_peer',None),('active_peer',key(8)),('peer_address',None),('version',True),
            ('version',3),('generation',True),('generation',2),('config_sha256','0'*64),
            ('retired_peers',[key(1)]),('unknown',True)]:
            h=copy.deepcopy(state);h[field]=value;case('invalid-'+field+'-'+str(len(values)),raw,vpn_peer.encoded(h),False)
        encoded=vpn_peer.encoded(state);case('duplicate-version',raw,b'{"version":1,'+encoded[1:],False)
        case('trailing-history',raw,encoded+b'{}',False)
        for name,changed in [('extra-hook',raw+b'PostUp = unexpected\n'),('extra-peer',raw+b'[Peer]\n'),
                ('subnet-route',raw.replace(b'/32',b'/24')),('zero-private-key',raw.replace(key(9).encode(),key(0).encode())),
                ('noncanonical-key',raw.replace(key(1).encode(),b'B'*43+b'='))]:
            h={**state,'config_sha256':vpn_peer.digest(changed)};case(name,changed,vpn_peer.encoded(h),False)
        c=(retired/vpn_peer.CONFIG).read_bytes();h=json.loads((retired/vpn_peer.HISTORY).read_bytes())
        for field,value in [('generation',1),('generation',5),('peer_address','10.77.0.1'),('peer_address','8.8.8.8'),('active_peer',key(1))]:
            altered={**h,field:value};case('retired-invalid-'+field+'-'+str(len(values)),c,vpn_peer.encoded(altered),False)
    return values


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    with args.output.open('x') as output:json.dump(corpus(),output,separators=(',',':'))
