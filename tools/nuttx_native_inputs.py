#!/usr/bin/env python3
"""Private native compiler-input capture for secret-free NuttX qualification.

Records real compilation, pre/post dependency hashes, compiler identity and object
bytes. Raw commands/absolute paths remain private. This is a compiled-input
superset, not proof of retained-link coverage, advisory closure or release trust.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid

SELF = Path(__file__).resolve()
MAX_FILE = 512 * 1024 * 1024


CATEGORIES = {'record','arguments','compiler-identity','dependency-probe','dependency-file',
              'source-mutation','object','origin','duplicate-output','sdk-identity','io','compiler-status'}


class Failure(ValueError):
    def __init__(self,category):
        assert category in CATEGORIES
        self.category=category
        super().__init__('native-input capture refused')


def require(value,category='record'):
    if not value: raise Failure(category)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()


def digest(path):
    with Path(path).open('rb') as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and 0<=before.st_size<=MAX_FILE)
        result=hashlib.sha256()
        while part:=source.read(1024*1024):result.update(part)
        after=os.fstat(source.fileno())
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns))
    return result.hexdigest()


def fresh_json(path,value):
    with Path(path).open('xb') as output:
        os.fchmod(output.fileno(),0o600);output.write(canonical(value)+b'\n')


def expanded(arguments,cwd,depth=0):
    require(depth<=8 and len(arguments)<=32768)
    result=[]
    for arg in arguments:
        require(type(arg) is str and '\x00' not in arg)
        if arg.startswith('@'):
            path=(cwd/arg[1:]).resolve()
            raw=path.read_bytes();require(len(raw)<=1024*1024)
            result.extend(expanded(shlex.split(raw.decode(),comments=False,posix=True),cwd,depth+1))
        else:result.append(arg)
    require(len(result)<=32768 and sum(map(len,result))<=2*1024*1024)
    return result


def probe_arguments(arguments):
    # Dependency target options (-MT/-MQ) accumulate, unlike -MF. Remove them
    # only from independent probes. The real compiler invocation stays intact,
    # including the build system's requested dependency file/targets.
    result=[];index=0
    while index<len(arguments):
        arg=arguments[index]
        if arg in ('-MF','-MT','-MQ'):
            require(index+1<len(arguments),'arguments');index+=2;continue
        if any(arg.startswith(flag) and len(arg)>len(flag) for flag in ('-MF','-MT','-MQ')):
            index+=1;continue
        if arg in ('-MD','-MMD','-MP','-MG'):
            index+=1;continue
        # Indirect dependency options require their own exact parser; refuse
        # instead of silently changing or misrecording their meaning.
        require(not (arg.startswith('-Wp,') and any(x.startswith('-M') for x in arg.split(',')[1:])), 'arguments')
        require(not (arg=='-Xpreprocessor' and index+1<len(arguments) and arguments[index+1].startswith('-M')), 'arguments')
        result.append(arg);index+=1
    return result


def compile_paths(arguments,cwd):
    if '-c' not in arguments or any(flag in arguments for flag in ['-M','-MM','-E','-S','-fsyntax-only']):return None
    arguments=probe_arguments(arguments)
    sources=[a for a in arguments if not a.startswith('-') and Path(a).suffix in
             ('.c','.cc','.cpp','.cxx','.C','.S')]
    require(len(sources)==1 and '-' not in arguments,'arguments')
    outputs=[]
    for i,arg in enumerate(arguments):
        if arg=='-o':require(i+1<len(arguments));outputs.append(arguments[i+1])
        elif arg.startswith('-o') and len(arg)>2:outputs.append(arg[2:])
    require(len(outputs)==1,'arguments')
    return (cwd/sources[0]).resolve(),(cwd/outputs[0]).resolve()


def dependencies(path,cwd):
    raw=Path(path).read_bytes();require(0<len(raw)<=4*1024*1024)
    text=raw.decode().replace('\\\n','')
    line=text.splitlines()[0]
    require(line.startswith('nxrs-native-object:'),'dependency-file')
    values=shlex.split(line.removeprefix('nxrs-native-object:'),comments=False,posix=True)
    require(0<len(values)<=8192)
    result=sorted(set(str((cwd/name).resolve()) for name in values))
    return result


def tool_unchanged(tool):
    path=Path(tool['path']);current=path.stat()
    actual=[current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns]
    require(actual==tool['stat'] or digest(path)==tool['sha256'],'compiler-identity')


def prepare(output,cc,cxx,origins):
    output=Path(output).resolve();require(not output.exists())
    values={}
    for name,path in origins.items():
        require(re.fullmatch('[a-z][a-z0-9-]{0,31}',name))
        path=Path(path).resolve();require(path.is_dir() and path!=Path(path.anchor))
        values[name]=str(path)
    require(values)
    tools={}
    for name,command in [('gcc',cc),('g++',cxx)]:
        if command is None:continue
        located=shutil.which(str(command));require(located is not None)
        path=Path(located).absolute();info=path.stat()
        tools[name]=dict(path=str(path),sha256=digest(path),
            stat=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns])
    output.mkdir(mode=0o700);(output/'records').mkdir(mode=0o700);(output/'bin').mkdir(mode=0o700)
    configuration=dict(schema=1,producer=str(SELF),producer_sha256=digest(SELF),origins=values,tools=tools)
    fresh_json(output/'configuration.json',configuration)
    for name in tools:
        wrapper=output/'bin'/name
        source=('#!'+sys.executable+'\nimport sys\nsys.path.insert(0,'+repr(str(SELF.parent))+')\n'
                'import nuttx_native_inputs as capture\n'
                'raise SystemExit(capture.capture_cli('+repr(str(output/'configuration.json'))+','+repr(name)+',sys.argv[1:]))\n')
        with wrapper.open('x') as stream:stream.write(source)
        wrapper.chmod(0o700)
        launcher=output/'bin'/(name+'-launcher')
        launcher_source=source.replace('capture.capture_cli(', 'capture.launcher_cli(')
        with launcher.open('x') as stream:stream.write(launcher_source)
        launcher.chmod(0o700)
    return configuration


def capture(configuration,name,arguments):
    configuration=Path(configuration).resolve();cfg=json.loads(configuration.read_bytes())
    require(cfg['schema']==1 and digest(SELF)==cfg['producer_sha256'] and str(SELF)==cfg['producer'])
    tool=cfg['tools'][name];tool_unchanged(tool)
    cwd=Path.cwd().resolve();args=expanded(arguments,cwd);paths=compile_paths(args,cwd)
    if paths is None:
        return subprocess.run([tool['path'],*args]).returncode
    source,obj=paths
    directory=configuration.parent/'records'/uuid.uuid4().hex;directory.mkdir(mode=0o700)
    pre,post=directory/'before.d',directory/'after.d'
    base=[tool['path'],*args]
    # Probe before/after the unmodified real compilation. Keep CMake/NuttX's
    # dependency file behavior intact and avoid additive -MT/-MQ targets.
    probe=[tool['path'],*probe_arguments(args)]
    result=subprocess.run([*probe,'-M','-MF',str(pre),'-MT','nxrs-native-object'],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    require(result.returncode==0,'dependency-probe')
    names=dependencies(pre,cwd);require(str(source) in names)
    before={path:digest(path) for path in names}
    result=subprocess.run(base)
    if result.returncode:return result.returncode
    result=subprocess.run([*probe,'-M','-MF',str(post),'-MT','nxrs-native-object'],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    require(result.returncode==0,'dependency-probe')
    require(names==dependencies(post,cwd) and before=={path:digest(path) for path in names},'source-mutation')
    tool_unchanged(tool);require(obj.is_file(),'object')
    record=dict(schema=1,source=str(source),object=str(obj),object_sha256=digest(obj),
                compiler_sha256=tool['sha256'],cwd=str(cwd),arguments=args,dependencies=before,
                producer_sha256=cfg['producer_sha256'])
    fresh_json(directory/'unit.json',record)
    return 0


def diagnostic(category,configuration=None,arguments=()):
    source='unknown'
    if configuration is not None:
        try:
            cfg=json.loads(Path(configuration).read_bytes())
            paths=compile_paths(expanded(arguments,Path.cwd()),Path.cwd())
            if paths:
                value=identity(paths[0],cfg['origins'])
                if len(value)<=192 and re.fullmatch('[A-Za-z0-9_.:/+-]+',value):source=value
        except (OSError,ValueError,KeyError):pass
    print('NXRS_NATIVE_INPUT_FAILURE category='+category+' source='+source,file=sys.stderr)


def capture_cli(configuration,name,arguments):
    try:
        status=capture(configuration,name,arguments)
        if status:diagnostic('compiler-status',configuration,arguments)
        return status
    except Failure as error:
        diagnostic(error.category,configuration,arguments);return 1
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        diagnostic('io',configuration,arguments);return 1


def launcher_cli(configuration,name,arguments):
    # CMake launchers receive the actual compiler as their first argument.
    # Verify it before forwarding, rather than treating it as a source operand.
    try:
        cfg=json.loads(Path(configuration).read_bytes())
        expected=Path(cfg['tools'][name]['path'])
        require(arguments and Path(arguments[0]).absolute()==expected
                and os.path.samefile(arguments[0],expected),'compiler-identity')
    except Failure as error:
        diagnostic(error.category);return 1
    except (OSError,ValueError,KeyError):
        diagnostic('compiler-identity');return 1
    return capture_cli(configuration,name,arguments[1:])


def identity(path,origins):
    path=Path(path).resolve()
    matches=[(len(Path(root).parts),name,str(path.relative_to(root)))
             for name,root in origins.items() if path.is_relative_to(root)]
    require(matches,'origin')
    _,name,relative=max(matches)
    require(relative not in ('','.'))
    return name+':'+relative


def sdk_identity(root):
    root=Path(root).resolve()
    def revision(directory,ref):
        value=subprocess.check_output(['git','-C',str(directory),'rev-parse',ref],stderr=subprocess.DEVNULL,text=True).strip()
        require(re.fullmatch('[0-9a-f]{40}',value));return value
    submodule=root/'components/mbedtls/mbedtls'
    expected=revision(root,'HEAD:components/mbedtls/mbedtls')
    actual=revision(submodule,'HEAD');require(expected==actual,'sdk-identity')
    def version(path,prefix):
        raw=path.read_text();values=[]
        for name in ['MAJOR','MINOR','PATCH']:
            matches=re.findall(r'^#define\s+'+prefix+name+r'\s+(\d+)\s*$',raw,re.M)
            require(len(matches)==1);values.append(matches[0])
        return '.'.join(values)
    idf=root/'components/esp_common/include/esp_idf_version.h'
    mbed=submodule/'include/mbedtls/build_info.h'
    return dict(revision=revision(root,'HEAD'),idf_version_macros=version(idf,'ESP_IDF_VERSION_'),
                idf_version_header_sha256=digest(idf),mbedtls_revision=actual,
                mbedtls_version_macros=version(mbed,'MBEDTLS_VERSION_'),mbedtls_version_header_sha256=digest(mbed),
                source_presence_does_not_prove_image_linkage=True)


def collect(directory,image,sdk_root=None):
    directory=Path(directory).resolve();cfg=json.loads((directory/'configuration.json').read_bytes())
    require(cfg['schema']==1 and cfg['producer_sha256']==digest(SELF))
    for tool in cfg['tools'].values():require(digest(tool['path'])==tool['sha256'])
    records=sorted((directory/'records').glob('*/unit.json'));require(0<len(records)<=20000)
    units=[];inputs={};objects=set()
    for path in records:
        record=json.loads(path.read_bytes());require(record['schema']==1 and record['producer_sha256']==cfg['producer_sha256'])
        require(record['compiler_sha256'] in [t['sha256'] for t in cfg['tools'].values()])
        obj=Path(record['object']);require(str(obj) not in objects,'duplicate-output');objects.add(str(obj))
        if obj.exists():require(digest(obj)==record['object_sha256'],'object')
        selected={}
        for name,value in record['dependencies'].items():
            require(digest(name)==value,'source-mutation')
            key=identity(name,cfg['origins']);require(key not in inputs or inputs[key]==value)
            inputs[key]=value;selected[key]=value
        units.append(dict(source=identity(record['source'],cfg['origins']),object_sha256=record['object_sha256'],
            compiler_sha256=record['compiler_sha256'],dependency_count=len(selected),
            dependencies_sha256=hashlib.sha256(canonical(selected)).hexdigest(),
            private_command_sha256=hashlib.sha256(canonical([record['cwd'],record['arguments']])).hexdigest()))
    require(len({u['source'] for u in units})>0)
    image=Path(image);require(image.is_file())
    return dict(schema=1,scope='actual-nuttx-native-compiler-input-superset',qualified=True,
                sdk=sdk_identity(sdk_root) if sdk_root else None,
                image_sha256=digest(image),producer_sha256=cfg['producer_sha256'],
                compilers={name:tool['sha256'] for name,tool in cfg['tools'].items()},inputs=inputs,units=units,
                compiled_units=len(units),retained_link_coverage_qualified=False,
                full_firmware_inventory_qualified=False,native_advisory_closure=False,
                rust_input_closure_qualified=False,physical_qualified=False,deployment_permitted=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('prepare');p.add_argument('--out',type=Path,required=True);p.add_argument('--cc',required=True)
    p.add_argument('--cxx');p.add_argument('--origin',action='append',required=True)
    p=commands.add_parser('collect');p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--image',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--sdk-root',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='prepare':
            origins={}
            for entry in args.origin:
                name,separator,value=entry.partition('=');require(separator and name not in origins);origins[name]=value
            prepare(args.out,args.cc,args.cxx,origins)
        else:fresh_json(args.report,collect(args.directory,args.image,args.sdk_root))
    except Failure as error:
        diagnostic(error.category);return 1
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        diagnostic('io');return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
