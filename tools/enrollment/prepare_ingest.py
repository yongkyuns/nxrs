#!/usr/bin/env python3
"""Stage an authenticated ingest config on the gateway; never contact the offline CA."""
import argparse
import ipaddress
from pathlib import Path
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
import issuer


def prepare(output, bundle_path, issuer_pin, minimum_generation, catalog_path, config_path,
            camera_id, camera_serial, client_serial, client_key_path, runtime_directory=None):
    output = issuer.absolute(output)
    runtime = issuer.absolute(runtime_directory) if runtime_directory is not None else output
    bundle = issuer.verify_bundle(issuer.read_file(issuer.absolute(bundle_path),issuer.MAX_STATE),
                                  issuer_pin,minimum_generation)
    catalog = issuer.parse(issuer.read_file(issuer.absolute(catalog_path),65536))
    gateway = issuer.scope(catalog,camera_id,bundle['site_id'])
    config = issuer.parse(issuer.read_file(issuer.absolute(config_path),8192))
    issuer.exact(config,'camera allow_insecure_camera_http rtsp stream_path publisher_user publisher_token_file ffmpeg publisher width height frame_timeout_ms' + (' media_sandbox' if 'media_sandbox' in config else '') + (' detections' if 'detections' in config else ''))
    if config.get('media_sandbox') is not None:
        issuer.require(isinstance(config['media_sandbox'],str))
        issuer.absolute(config['media_sandbox'])
    camera = next(c for c in catalog['cameras'] if c['camera_id']==camera_id)
    issuer.require(config['stream_path']==camera['stream_path'] and any(
        p['username']==config['publisher_user'] and p['camera_id']==camera_id for p in catalog['publishers']))
    host, separator, port = config['camera'].rpartition(':')
    issuer.require(separator and str(ipaddress.IPv4Address(host))==host and port.isascii() and port.isdecimal()
                   and 1<=int(port)<=65535)
    if 'detections' in config:
        metadata = config['detections']
        issuer.exact(metadata,'control camera_id')
        issuer.require(metadata['camera_id']==camera_id and isinstance(metadata['control'],str))
        endpoint, separator, service = metadata['control'].rpartition(':')
        address = ipaddress.IPv4Address(endpoint)
        issuer.require(separator and str(address)==endpoint and address.is_loopback
                       and service.isascii() and service.isdecimal() and 1<=len(service)<=5
                       and 1<=int(service)<=65535)
        for field in ['camera','rtsp']:
            other, separator, other_port = config[field].rpartition(':')
            issuer.require(separator and (address,int(service))!=(ipaddress.IPv4Address(other),int(other_port)))
    selected = {}
    now = issuer.instant()
    for role, serial, purpose in [('camera',camera_serial,ExtendedKeyUsageOID.SERVER_AUTH),
                                   ('ingest',client_serial,ExtendedKeyUsageOID.CLIENT_AUTH)]:
        entry = next((e for e in bundle['entries'] if e['serial']==serial),None)
        issuer.require(entry is not None and entry['role']==role and entry['camera_id']==camera_id
                       and entry['gateway_id']==gateway and entry['revoked_at'] is None)
        cert = issuer.certificate(entry['certificate'])
        issuer.require(cert.not_valid_before_utc<=now<cert.not_valid_after_utc
                       and list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)==[purpose])
        if role=='camera': issuer.require(entry['ip']==host)
        selected[role] = entry['certificate'].encode()
    secret = issuer.read_file(issuer.absolute(client_key_path),32768)
    issuer.one_pem(secret,b'PRIVATE KEY',32768)
    key = serialization.load_pem_private_key(secret,None)
    issuer.require(issuer.spki(issuer.p256(key.public_key())) ==
                   issuer.spki(issuer.certificate(selected['ingest']).public_key()))
    config.update(allow_insecure_camera_http=False,camera_tls={
        'site_id':bundle['site_id'],'camera_id':camera_id,'gateway_id':gateway,
        'issuer_sha256':issuer_pin,'enrollment_generation':bundle['generation'],
        'ca_certificate_file':str(runtime/'camera-ca.pem'),
        'camera_certificate_file':str(runtime/'camera-peer.pem'),
        'client_certificate_file':str(runtime/'camera-client.pem'),
        'client_key_file':str(runtime/'camera-client.key'),'crl_file':str(runtime/'camera-crl.pem')})
    files = {'camera-ca.pem':bundle['ca_certificate'].encode(),'camera-peer.pem':selected['camera'],
             'camera-client.pem':selected['ingest'],'camera-client.key':secret,
             'camera-crl.pem':bundle['revocations'].encode(),'ingest.json':issuer.canonical(config)+b'\n'}
    if runtime == Path('/run/nxrs-ingest'):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'gateway/deploy'))
        import ingest_service
        files['nxrs-ingest.service'] = ingest_service.service(camera_tls=True).encode()
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    fd = os.open(output,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        issuer.private_stat(fd,directory=True)
        issuer.new_file(fd,'.incomplete',b'')
        for name,data in files.items(): issuer.new_file(fd,name,data)
        os.fsync(fd); os.unlink('.incomplete',dir_fd=fd); os.fsync(fd)
    finally:
        os.close(fd)
    issuer.sync_parent(output)
    return output/'ingest.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['output','bundle','catalog','config','client-key']:
        parser.add_argument('--'+name,required=True,type=Path)
    for name in ['issuer-sha256','camera','camera-serial','client-serial']:
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--minimum-generation',required=True,type=int)
    parser.add_argument('--runtime-directory',type=Path,default=Path('/run/nxrs-ingest'))
    args = parser.parse_args()
    try:
        issuer.qualified_runtime()
        prepare(args.output,args.bundle,args.issuer_sha256,args.minimum_generation,args.catalog,args.config,
                args.camera,args.camera_serial,args.client_serial,args.client_key,args.runtime_directory)
        print('Authenticated ingest staged. Validate with --check-config --require-camera-tls before explicit installation.')
    except Exception:
        parser.exit(1,'Authenticated ingest staging failed; check approved scope, current bundle, private local key and new output.\n')


if __name__=='__main__': main()
