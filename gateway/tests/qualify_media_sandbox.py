#!/usr/bin/env python3
"""Actual isolated child controls on disposable hosted Linux only; no raw export."""
import argparse, ctypes, hashlib, json, os, platform, re, shutil, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
INPUTS=('gateway/Cargo.toml','gateway/Cargo.lock','gateway/media-sandbox/Cargo.toml',
 'gateway/media-sandbox/src/lib.rs','gateway/media-sandbox/src/main.rs',
 'gateway/media-sandbox/src/platform/linux/mod.rs','gateway/media-sandbox/src/platform/linux/syscall.rs',
 'gateway/tests/media_sandbox/probe.c','gateway/tests/qualify_media_sandbox.py','tests/device-identity/frame.jpg',
 '.github/workflows/monitoring-ingest.yml')
def require(value):
 if not value:raise ValueError('media isolation qualification refused')
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def hosted():
 require(sys.platform=='linux' and os.environ.get('GITHUB_ACTIONS')=='true'
         and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and platform.machine() in ('x86_64','aarch64'))
def execute(args):
 hosted() # Before files, commands or changing this owned fixture process's ptrace permission.
 inputs={p:digest(ROOT/p) for p in INPUTS}
 require(not args.work.exists());args.work.mkdir(mode=0o700);os.umask(0o077)
 launcher=args.launcher.resolve();ffmpeg=(args.ffmpeg or Path(shutil.which('ffmpeg'))).resolve()
 probe=args.work/'probe'
 subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-O2',str(ROOT/'gateway/tests/media_sandbox/probe.c'),'-o',str(probe)],check=True,capture_output=True,timeout=30)
 print('MEDIA_ISOLATION_PHASE capability',flush=True)
 checked=subprocess.run([launcher,'--check'],capture_output=True,timeout=5)
 require(checked.returncode==0 and checked.stdout==b'NXRS_MEDIA_ISOLATION_READY\n' and not checked.stderr)
 lib=ctypes.CDLL(None,use_errno=True);lib.prctl.argtypes=[ctypes.c_int,*([ctypes.c_ulong]*4)];PR_SET_PTRACER=0x59616d61
 canary=ctypes.create_string_buffer(b'q'*32)
 observations={}
 for isolated in (False,True):
  name='isolated' if isolated else 'baseline';print('MEDIA_ISOLATION_PHASE '+name,flush=True);base=args.work/name;base.mkdir(mode=0o700)
  work=base/'job';work.mkdir(mode=0o700);secret=base/'secret';secret.write_bytes(b's'*32)
  link=work/'link';link.symlink_to(secret)
  inherited=os.open(secret,os.O_RDONLY)
  command=([str(launcher),'--work',str(work),'--'] if isolated else [])+[str(probe),str(secret),str(link),str(base/'outside'),str(work/'allowed'),str(work/'hardlink'),str(os.getpid()),format(ctypes.addressof(canary),'x'),str(inherited)]
  process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(inherited,),env={'PATH':'/usr/bin:/bin','LANG':'C'})
  try:
   # Only the owned child PID may inspect this disposable canary process. This
   # supplies a real positive control even under a host ptrace_scope policy.
   require(lib.prctl(PR_SET_PTRACER,ctypes.c_ulong(process.pid),0,0,0)==0)
   output,error=process.communicate(b'g',timeout=8)
   require(process.returncode==0 and not error and len(output)<=512)
  finally:
   lib.prctl(PR_SET_PTRACER,0,0,0,0);os.close(inherited)
   if process.poll() is None:process.kill();process.wait(timeout=3)
  require(re.fullmatch(rb'(?:[a-z_]+=[01] ){13}[a-z_]+=[01]\n',output))
  values={k:int(v) for k,v in (part.split('=') for part in output.decode().split())}
  require(set(values)=={'secret','symlink','outside_write','work_write','parent_memory','inherited','tcp','udp','unix','signal','hardlink','fork','clone','clone_new'})
  wanted={key:(1 if not isolated or key=='work_write' else 0) for key in values}
  require(values==wanted);observations[name]=values
 print('MEDIA_ISOLATION_PHASE encoding',flush=True)
 # Execute both production output formats inside the same launcher. Annotated
 # publication needs the raw H264 muxer, independently of its demuxer.
 image=(ROOT/'tests/device-identity/frame.jpg').read_bytes()*16
 annexb_units=0
 for media_format,suffix in [('mpegts','ts'),('h264','h264')]:
  arguments=['-nostdin','-hide_banner','-loglevel','quiet','-threads','2','-f','image2pipe','-c:v','mjpeg','-framerate','30','-i','pipe:0','-an','-c:v','libx264','-threads','2','-preset','ultrafast','-pix_fmt','yuv420p','-bf','0']
  if media_format=='h264':arguments+=['-x264-params','aud=1']
  arguments+=['-f',media_format,'pipe:1']
  encoded=subprocess.run([launcher,'--',ffmpeg,*arguments],input=image,capture_output=True,timeout=15,env={'PATH':'/usr/bin:/bin','LANG':'C'})
  require(encoded.returncode==0 and not encoded.stderr and 0<len(encoded.stdout)<=2*1024*1024)
  if media_format=='h264':
   annexb_units=sum((match[1][0]&31)==9 for match in re.finditer(rb'\x00\x00\x00?\x01(.)',encoded.stdout,re.DOTALL))
   require(annexb_units==16)
  stream=args.work/('encoded.'+suffix);stream.write_bytes(encoded.stdout)
  decoded=subprocess.run([str(args.ffprobe or Path(shutil.which('ffprobe'))),'-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=codec_name,width,height,nb_read_frames','-of','json',stream],capture_output=True,timeout=10)
  require(decoded.returncode==0)
  video=json.loads(decoded.stdout)['streams'];require(video and all(s['codec_name']=='h264' and s['width']==320 and s['height']==240 and int(s['nb_read_frames'])==16 for s in video))
 require(inputs=={p:digest(ROOT/p) for p in INPUTS})
 head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
 require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
 report=dict(host_machine=platform.machine(),kernel_release=platform.release(),required_landlock_abi=3, schema=1,scope='disposable-linux-media-process-isolation',qualified=True,inputs=inputs,checkout=dict(clean=True,commit=head),
  executables=dict(launcher=digest(launcher),probe=digest(probe),ffmpeg=digest(ffmpeg)),controls=observations,encoded_frames=16,
  annexb_encoded_frames=16,annexb_access_units=annexb_units,
  secrets_in_report=False,physical_qualified=False,deployment_permitted=False,fips_validated=False)
 raw=(json.dumps(report,sort_keys=True,separators=(',',':'))+'\n').encode();require(len(raw)<=65536)
 (args.work/'qualification.json').write_bytes(raw)
 print('PASS: actual file/FD/process/network/signalling/process-creation confinement and sixteen decoded frames per MPEG-TS/Annex-B format')
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--launcher',type=Path,required=True);p.add_argument('--work',type=Path,required=True);p.add_argument('--ffmpeg',type=Path);p.add_argument('--ffprobe',type=Path);a=p.parse_args()
 try:execute(a)
 except Exception:p.exit(1,'Media isolation qualification refused; no pass report was produced.\n')
if __name__=='__main__':main()
