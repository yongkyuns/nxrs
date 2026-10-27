"""Exact source/image joins for the owned service guest's XML ABI witness.

This module validates public fixture contracts only. Guest admission and native
execution stay in service_boot_guest; host preparation stays in service_boot.
"""
from pathlib import Path
import re

RUNTIME=Path('/usr/lib/nxrs-gateway-system-xml-qualification')
LIBRARY=RUNTIME/'lib/libexpat.so.1.13.0'
QUALIFICATION=RUNTIME/'qualification.json'
LINK=Path('/usr/lib/x86_64-linux-gnu/libexpat.so.1')
LINK_TARGET='../nxrs-gateway-system-xml-qualification/lib/libexpat.so.1.13.0'
PROBE=Path('/var/lib/service-boot/bin/xml-probe')
SCOPE='owned-service-boot-system-xml-abi'

def require(value,category):
    if not value:raise ValueError(category)

def validate_plan(plan):
    proof=plan['system_xml_runtime']
    require(isinstance(proof,dict) and set(proof)=={'qualification_sha256','library_sha256'} and
        all(isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) for v in proof.values()),'exact-service-xml-proof')
    files=plan['files']
    require(files.get(str(QUALIFICATION))==proof['qualification_sha256'] and
        files.get(str(LIBRARY))==proof['library_sha256'] and
        isinstance(files.get(str(PROBE)),str) and re.fullmatch('[a-f0-9]{64}',files[str(PROBE)]),'service-xml-file-joins')
    return proof

def validate(value,plan):
    fields={'schema','scope','proof','probe_sha256','current_python_exact_image','c_abi_qualified',
        'wrong_image_refused','unresolved_published_advisories','full_native_advisory_closure','deployment_permitted'}
    require(isinstance(value,dict) and set(value)==fields and type(value['schema']) is int and value['schema']==1 and
        value['scope']==SCOPE,'exact-guest-xml-report')
    require(value['proof']==validate_plan(plan) and value['probe_sha256']==plan['files'][str(PROBE)],'same-guest-xml-images')
    require(all(value[k] is True for k in ('current_python_exact_image','c_abi_qualified','wrong_image_refused')) and
        value['unresolved_published_advisories']==['CVE-2025-66382'] and
        value['full_native_advisory_closure'] is value['deployment_permitted'] is False,'qualified-guest-xml-scope')
    return value
