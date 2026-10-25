#!/usr/bin/env python3
"""Join executed SDK/isolation controls and exercise this exact media runtime."""
import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import sys
import prepare


def run(args):
    native=prepare.native;native.hosted()
    root=prepare.ROOT;runtime=prepare.RUNTIME
    work=args.work.resolve();native.require(not work.exists(),'new-media-qualification-work');work.mkdir(mode=0o700)
    def read(path):
        raw=path.read_bytes();native.require(len(raw)<=65536,'bounded-media-receipt');return raw,native.parse(raw)
    build_raw,build=read(runtime/'build.json');reader_raw,reader=read(args.reader_report);isolation_raw,isolation=read(args.isolation_report)
    native.require(build['scope']=='hosted-minimized-gateway-media-build-not-release-admission' and build['build_complete'] is True and build['qualified'] is False,'actual-minimal-build-required')
    native.require(reader['scope']=='actual-host-rtsp-reader-remux-and-credential-controls' and reader['qualified'] is True
        and reader['tests_run']==4 and reader['native_decoder_open_observer_executed'] is True,'complete-sdk-controls-required')
    native.require(isolation['scope']=='disposable-linux-media-process-isolation' and isolation['qualified'] is True
        and isolation['encoded_frames']==16,'complete-media-isolation-required')
    head=native.command(['/usr/bin/git','-C',str(root),'rev-parse','HEAD']).strip()
    native.require(all(r['checkout']==dict(commit=head,clean=True) for r in [build,reader,isolation]),'same-clean-runtime-checkout')
    inputs={}
    for report in [build,reader,isolation]:
        for name,value in report['inputs'].items():
            path=PurePosixPath(name)
            native.require(not path.is_absolute() and path.parts and '..' not in path.parts and path.as_posix()==name
                and isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value),'canonical-runtime-source-identity')
            native.require(name not in inputs or inputs[name]==value,'consistent-runtime-source-receipts');inputs[name]=value
    native.require(set(inputs)==prepare.evidence.source_names(root),'complete-qualified-runtime-source-inventory')
    for name,value in inputs.items():native.require(prepare.digest(root/name)==value,'current-qualified-runtime-source')
    native.require(build['inputs']==prepare.inputs(),'current-build-source-closure')
    files=build['runtime_files']
    for name,value in files.items():
        path=PurePosixPath(name)
        native.require(not path.is_absolute() and path.parts and '..' not in path.parts and path.as_posix()==name
            and isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value),'canonical-runtime-file-identity')
    native.require(all(prepare.digest(runtime/n)==h for n,h in files.items()),'unchanged-built-runtime')
    ffmpeg=runtime/'bin/ffmpeg';ffprobe=runtime/'bin/ffprobe'
    native.require(isolation['executables']['ffmpeg']==prepare.digest(ffmpeg),'same-isolated-media-runtime')
    sdk={}
    for name in ['publisher','reader']:
        path=root/'gateway/target/debug'/('nxrs-rtsp-'+name)
        native.require(reader['executables'][name]==prepare.digest(path),'same-executed-sdk-binary')
        links=native.dependencies(path)
        avlibs=[p for p in links if p.name.startswith(('libavcodec.','libavformat.','libavutil.','libx264.'))]
        native.require(len(avlibs)==4 and all(p.is_relative_to(runtime/'lib') and files[str(p.relative_to(runtime))]==prepare.digest(p) for p in avlibs),'sdk-links-exact-custom-media-libraries')
        sdk[name]=dict(sha256=prepare.digest(path),libraries={p.name:prepare.digest(p) for p in avlibs})
    runner=prepare.load('media_control_runner','gateway/fuzz/run.py')
    def execute(command,phase):
        status,raw=runner.execute(command,work,phase,{'PATH':'/usr/bin:/bin','LANG':'C'},30,cwd=work)
        native.require(status==0,'media-runtime-'+phase);return raw
    image=work/'images.jpg';image.write_bytes((root/'tests/device-identity/frame.jpg').read_bytes()*96)
    stream=work/'part.ts'
    execute([ffmpeg,'-nostdin','-v','error','-threads','2','-f','image2pipe','-c:v','mjpeg','-framerate','30','-i',image,
        '-an','-c:v','libx264','-threads','2','-preset','ultrafast','-tune','zerolatency','-pix_fmt','yuv420p','-bf','0',
        '-f','mpegts',stream],'encode')
    (work/'inputs.txt').write_text("file 'part.ts'\n")
    clip=work/'clip.mp4';thumbnail=work/'thumbnail.jpg'
    execute([ffmpeg,'-nostdin','-v','error','-threads','2','-filter_threads','1','-f','concat','-safe','1',
        '-protocol_whitelist','file','-i',work/'inputs.txt','-map','0:v:0','-an','-c:v','copy','-movflags','+faststart',clip,
        '-map','0:v:0','-frames:v','1','-vf','scale=320:-2','-q:v','5',thumbnail],'finalize-clip-and-thumbnail')
    decoded=native.parse(execute([ffprobe,'-v','error','-count_frames','-select_streams','v:0','-show_entries',
        'stream=codec_name,width,height,nb_read_frames','-of','json',clip],'decode-clip'))['streams']
    native.require(len(decoded)==1 and decoded[0]==dict(codec_name='h264',width=320,height=240,nb_read_frames='96'),'complete-clip-decode')
    execute([ffmpeg,'-nostdin','-v','error','-i',clip,'-an','-c:v','rawvideo','-f','null','-'],'complete-clip-raw-decode')
    native.require(1024<clip.stat().st_size<=33554432 and 4<thumbnail.stat().st_size<=153600,'bounded-clip-thumbnail')
    native.require(all(prepare.digest(runtime/n)==h for n,h in files.items()) and all(prepare.digest(root/n)==h for n,h in inputs.items()),'unchanged-qualified-runtime')
    native.require(not native.command(['/usr/bin/git','-C',str(root),'status','--porcelain']).strip(),'clean-qualified-source')
    result=dict(schema=1,scope='actual-minimized-host-gateway-media-sdk-isolation-and-clip',qualified=True,
        checkout=dict(commit=head,clean=True),inputs=inputs,build_sha256=hashlib.sha256(build_raw).hexdigest(),
        reader_sha256=hashlib.sha256(reader_raw).hexdigest(),isolation_sha256=hashlib.sha256(isolation_raw).hexdigest(),sdk=sdk,
        decoded_clip_frames=96,clip_bytes=clip.stat().st_size,clip_sha256=prepare.digest(clip),thumbnail_sha256=prepare.digest(thumbnail),
        unchanged_isolation_grants=True,native_decoder_open_observer_executed=True,
        encrypted_recording_composition_qualified=False,sdk_mutual_tls_composition_qualified=False,
        upstream_advisory_closure=False,full_native_advisory_closure=False,optimized_assembly_fuzz_qualified=False,complete_native_parser_coverage=False,physical_qualified=False,fips_validated=False,deployment_permitted=False)
    raw=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode();native.require(len(raw)<=65536,'bounded-runtime-qualification')
    with args.report.open('xb') as output:output.write(raw)
    print('PASS: exact minimized SDK/isolation and 96-frame finalized clip; broader security gates remain open')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['work','reader-report','isolation-report','report']:p.add_argument('--'+name,type=Path,required=True)
    try:run(p.parse_args())
    except Exception as error:
        category=str(error) if isinstance(error,prepare.native.Refusal) and __import__('re').fullmatch('[a-z0-9-]{1,100}',str(error)) else type(error).__name__
        p.exit(1,'Minimized runtime qualification refused ('+category+').\n')


if __name__=='__main__':main()
