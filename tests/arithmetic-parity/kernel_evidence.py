"""Freeze kernel archives, allowing only the verified NuttX uname timestamp."""
import hashlib
from pathlib import Path
import re
import subprocess


def kernel_inventory(tree, prefix):
    result = {}
    for path in sorted((tree / "nuttx/staging").glob("*.a")):
        if path.name == "libapps.a": continue
        if path.name != "libc.a":
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            names = subprocess.check_output([prefix+"ar", "t", path],text=True).splitlines()
            if names.count("lib_utsname.o") != 1 or len(names) != len(set(names)):
                raise ValueError("ambiguous libc archive inventory")
            result[path.name] = {name:hashlib.sha256(subprocess.check_output([prefix+"ar","p",path,name])).hexdigest()
                                 for name in names if name != "lib_utsname.o"}
    return result


def freeze_utsname(tree, prefix, out, name):
    path = out / name
    path.write_bytes(subprocess.check_output([prefix+"ar","p",tree/"nuttx/staging/libc.a","lib_utsname.o"]))
    return path


def timestamp_only(before, after, prefix):
    normalized = []
    for path in (before,after):
        table = subprocess.check_output([prefix+"readelf","-W","-S",path],text=True)
        matches = re.findall(r'\]\s+\.data\.g_version\s+PROGBITS\s+[0-9a-f]+\s+([0-9a-f]+)\s+([0-9a-f]+)',table)
        if len(matches) != 1: raise ValueError("uname timestamp section missing or ambiguous")
        offset,size = (int(value,16) for value in matches[0])
        data=path.read_bytes()
        if not re.fullmatch(rb'0 [A-Za-z]{3} [ 0-9][0-9] [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}\x00',data[offset:offset+size]):
            raise ValueError("unexpected uname build date encoding")
        normalized.append(data[:offset]+bytes(size)+data[offset+size:])
    if normalized[0] != normalized[1]: raise ValueError("uname changed outside build timestamp")
