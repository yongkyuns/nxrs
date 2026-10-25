#!/usr/bin/env python3
"""Build the minimized gateway media runtime only on disposable hosted Ubuntu."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=Path('/usr/lib/nxrs-gateway-media-qualification')
INPUTS=('tools/gateway-media/prepare.py','tools/gateway-media/dependencies.json','tools/gateway-media/README.md',
        'tests/host/test_gateway_media.py','.github/workflows/gateway-media-runtime.yml',
        'security/native-media-dependencies.json','tools/native-media/qualify.py',
        'gateway/fuzz/run.py','tools/native-packages/ubuntu.py','tools/ci-apt.py')
SELECTIONS=dict(decoder=['h264','mjpeg'],encoder=['libx264','mjpeg','rawvideo'],
    parser=['h264','mjpeg'],demuxer=['concat','h264','image2','image2pipe','mov','mpegts','rtsp'],
    muxer=['image2','image2pipe','mov','mp4','mpegts','null','rawvideo','rtsp','segment'],
    filter=['format','fps','null','scale'],protocol=['file','pipe','tcp'])


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


native=load('gateway_ubuntu','tools/native-packages/ubuntu.py')
extractor=load('gateway_native_extract','tools/native-media/qualify.py')


def require(value,category):native.require(value,category)


def digest(path):return native.file_digest(path)


def inputs():return {n:digest(ROOT/n) for n in INPUTS}


def configuration(prefix):
    flags=['--prefix='+str(prefix),'--libdir='+str(prefix/'lib'),'--disable-everything',
           '--disable-autodetect','--disable-doc','--disable-debug','--disable-static','--enable-shared',
           '--enable-rpath','--enable-gpl','--enable-libx264','--enable-ffmpeg','--enable-ffprobe',
           '--disable-ffplay','--disable-avdevice','--disable-swresample',
           '--enable-avfilter','--enable-swscale','--disable-vulkan','--disable-vaapi','--disable-vdpau',
           '--disable-cuda-llvm','--disable-cuvid','--disable-nvdec','--disable-nvenc',
           '--disable-audiotoolbox','--disable-videotoolbox',
           '--extra-ldflags=-Wl,-rpath,'+str(prefix/'lib')]
    return flags+['--enable-'+kind+'='+','.join(names) for kind,names in SELECTIONS.items()]


def resolved(build):
    values=dict(re.findall(r'^#define (CONFIG_[A-Z0-9_]+) ([01])$',
        (build/'config.h').read_text()+'\n'+(build/'config_components.h').read_text(),re.M))
    # Some containers/CLI components select small internal prerequisites. Keep
    # the resolved inventory explicit and refuse broad codec/device admission.
    allowed=dict(decoder={'h264','mjpeg'},encoder={'libx264','mjpeg','rawvideo'},
        parser={'h264','mjpeg','ac3'},hwaccel=set(),indev=set(),outdev=set(),
        protocol={'file','pipe','tcp','udp','rtp','http','httpproxy'},
        demuxer=set(SELECTIONS['demuxer']),muxer=set(SELECTIONS['muxer'])|{'rtp'},
        filter=set(SELECTIONS['filter'])|{'buffer','buffersink','abuffer','abuffersink','aformat','anull','atrim','trim','hflip','vflip','transpose','rotate'})
    observed={}
    for kind,names in allowed.items():
        suffix='_'+kind.upper()
        selected={n[len('CONFIG_'):-len(suffix)].lower() for n,v in values.items() if v=='1' and n.endswith(suffix)}
        if not selected<=names:
            print('GATEWAY_MEDIA_CONFIGURATION '+json.dumps(dict(kind=kind,unexpected=sorted(selected-names))),flush=True)
        require(selected<=names,'bounded-resolved-'+kind)
        if kind in SELECTIONS:require(set(SELECTIONS[kind])<=selected,'required-resolved-'+kind)
        observed[kind]=sorted(selected)
    require(all(values.get('CONFIG_'+name)=='0' for name in ['AUTODETECT','AVDEVICE','SWRESAMPLE','OPENSSL','GNUTLS','LIBXML2','LIBASS','SDL2']), 'no-unselected-external-families')
    return dict(selected=observed,configuration_sha256={n:digest(build/n) for n in ['config.h','config_components.h','ffbuild/config.mak']})


def fetch(pin,work,name):
    require(pin['url'].startswith('https://'),'https-source')
    with urllib.request.urlopen(pin['url'],timeout=45) as source:raw=source.read(32*1024*1024+1)
    require(len(raw)<=32*1024*1024 and hashlib.sha256(raw).hexdigest()==pin['sha256'],'pinned-source-archive')
    path=work/(name+'.tar');path.write_bytes(raw)
    return extractor.extract(path,work,pin['root'])


def run(work):
    native.hosted() # Before files, sudo, packages, network or compilation.
    require(os.geteuid()!=0 and not os.path.lexists(work) and not os.path.lexists(RUNTIME),'new-unprivileged-owned-build')
    head=native.command(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD']).strip()
    require(not native.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'clean-build-source')
    before=inputs();work.mkdir(mode=0o700);os.umask(0o077)
    env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','HOME':str(work),
         'PKG_CONFIG_PATH':str(RUNTIME/'lib/pkgconfig'),'SOURCE_DATE_EPOCH':'1792368000'}
    runner=load('gateway_build_runner','gateway/fuzz/run.py')
    def execute(command,phase,seconds=600,cwd=None):
        status,raw=runner.execute(command,work,phase,env,seconds,cwd=cwd or work)
        require(status==0,'gateway-build-'+phase);return raw
    # A new, exclusively owned public runtime under the existing library read
    # hierarchy preserves Landlock grants and needs no loader environment escape.
    execute(['sudo','-n','install','-d','-m','0755','-o',str(os.getuid()),'-g',str(os.getgid()),str(RUNTIME)],'owned-runtime',10)
    recipe=native.parse((ROOT/'tools/gateway-media/dependencies.json').read_bytes())
    require(recipe['schema']==1,'recipe-schema')
    ff=native.parse((ROOT/'security/native-media-dependencies.json').read_bytes())['ffmpeg']
    sources={}
    for name,pin in [('x264',recipe['x264']),('ffmpeg',ff)]:
        print('GATEWAY_MEDIA_PHASE source-'+name,flush=True);sources[name]=fetch(pin,work,name)
    require((sources['ffmpeg']/'RELEASE').read_text().strip()==ff['version'],'actual-ffmpeg-release')
    print('GATEWAY_MEDIA_PHASE x264-build',flush=True)
    xflags=['--prefix='+str(RUNTIME),'--libdir='+str(RUNTIME/'lib'),'--enable-shared','--disable-cli',
            '--disable-opencl','--disable-lsmash','--disable-swscale','--disable-ffms','--enable-pic']
    execute([sources['x264']/'configure',*xflags],'x264-config',cwd=sources['x264'])
    execute(['make','-j2','lib-shared'],'x264-build',cwd=sources['x264']);execute(['make','install-lib-shared','install-lib-dev'],'x264-install',cwd=sources['x264'])
    build=work/'ffmpeg-build';build.mkdir()
    print('GATEWAY_MEDIA_PHASE ffmpeg-build',flush=True)
    flags=configuration(RUNTIME);execute([sources['ffmpeg']/'configure',*flags],'ffmpeg-config',cwd=build)
    selection=resolved(build)
    execute(['make','-j2'],'ffmpeg-build',cwd=build);execute(['make','install'],'ffmpeg-install',cwd=build)
    # chmod only this newly created public runtime, never targets of symlinks.
    for path in [RUNTIME,*RUNTIME.rglob('*')]:
        if not path.is_symlink():path.chmod(0o755 if path.is_dir() or path.parent==RUNTIME/'bin' else 0o644)
    files={str(p.relative_to(RUNTIME)):digest(p) for p in sorted(RUNTIME.rglob('*')) if p.is_file() and not p.is_symlink()}
    require(files and len(files)<=512,'bounded-runtime-files')
    roots={}
    for name in ['ffmpeg','ffprobe']:
        path=RUNTIME/'bin'/name;links=native.dependencies(path)
        require(all(p.is_relative_to(RUNTIME/'lib') or p.name in ['libc.so.6','libm.so.6','libpthread.so.0','libdl.so.2','librt.so.1','libgcc_s.so.1','ld-linux-x86-64.so.2'] for p in links),'minimal-native-runtime-graph')
        roots[name]=dict(sha256=digest(path),libraries={str(p.relative_to(RUNTIME)) if p.is_relative_to(RUNTIME) else p.as_posix():digest(p) for p in sorted(links)})
    require(before==inputs() and files=={n:digest(RUNTIME/n) for n in files},'unchanged-media-source-runtime')
    require(not native.command(['/usr/bin/git','-C',str(ROOT),'status','--porcelain']).strip(),'unchanged-clean-build-source')
    result=dict(schema=1,scope='hosted-minimized-gateway-media-build-not-release-admission',qualified=False,build_complete=True,
        checkout=dict(commit=head,clean=True),inputs=before,sources=dict(ffmpeg=ff,x264=recipe['x264']),
        configure=flags,x264_configure=xflags,resolved=selection,runtime_files=files,roots=roots,
        x264_configuration_sha256=digest(sources['x264']/'config.h'),
        upstream_advisory_closure=False,full_native_advisory_closure=False,package_payload_authenticated=False,
        physical_qualified=False,fips_validated=False,deployment_permitted=False)
    # The prefix is public and disposable; no logs, source archives or media enter it.
    raw=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode();require(len(raw)<=65536,'bounded-public-build-receipt')
    (RUNTIME/'build.json').write_bytes(raw);(RUNTIME/'build.json').chmod(0o644)
    print('PASS: minimized gateway media build; integration and advisory admission remain separate')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,required=True);args=p.parse_args()
    try:run(args.work.resolve())
    except Exception as error:
        category=str(error) if isinstance(error,native.Refusal) and re.fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        p.exit(1,'Minimized gateway media build refused ('+category+'); no admission implied.\n')


if __name__=='__main__':main()
