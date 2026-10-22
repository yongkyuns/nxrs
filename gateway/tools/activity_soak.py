#!/usr/bin/env python3
"""Bounded unattended observation; success requires the entire requested interval.

Reads authenticated activity metadata and owned process counters. It never
opens USB, restarts/repairs a device, changes trust, or substitutes video.
Private numeric evidence is kept outside Git. Interruptions are retained.
"""
import argparse
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def private_read(path, maximum):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError('private bounded input required')
    return path.read_bytes()


def atomic(path, value):
    pending = path.with_suffix('.pending')
    with open(pending, 'w', opener=lambda p,f: os.open(p,f,0o600)) as file:
        json.dump(value,file); file.write('\n'); file.flush(); os.fsync(file.fileno())
    os.replace(pending,path)


def resource(pid):
    if not isinstance(pid,int) or pid <= 1:
        raise ValueError('invalid owned PID')
    if sys.platform.startswith('linux'):
        values = {}
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith(('VmRSS:', 'Threads:')):
                key,value,*_ = line.split(); values[key.rstrip(':')] = int(value)
        return dict(rss_bytes=values['VmRSS']*1024, threads=values['Threads'])
    rss = subprocess.check_output(['ps','-p',str(pid),'-o','rss='],text=True,timeout=5).strip()
    threads = subprocess.check_output(['ps','-M','-p',str(pid),'-o','pid='],text=True,timeout=5).splitlines()
    if not rss or not threads:
        raise ValueError('process counters unavailable')
    return dict(rss_bytes=int(rss)*1024,threads=len(threads))


class Progress:
    def __init__(self, seconds, started):
        self.seconds, self.started = seconds, started
        self.samples = 0
        self.failures = []
        self.last_counter = None
        self.last_advance = None
        self.max_gap = 0
        self.peaks = {}
        self.baselines = {}
        self.initial_dropped = None

    def sample(self, now, page, processes):
        self.samples += 1
        health = page.get('health') or {}
        counter = health.get('frames_observed')
        stamp = health.get('last_frame_at_ms')
        observed = page.get('observed_at_ms',0)
        dropped=health.get('clips_dropped',0)
        if self.initial_dropped is None:
            self.initial_dropped=dropped
        if dropped>self.initial_dropped or health.get('state') in ['storage_full','recording_error','stopped']:
            self.failures.append(dict(elapsed=round(now-self.started,1),category='automatic_recording_failure'))
        if not isinstance(counter,int) or not isinstance(stamp,int) or observed < stamp or observed-stamp > 15000:
            self.failures.append(dict(elapsed=round(now-self.started,1), category='camera_frames_not_recent'))
        if self.last_counter is not None and counter == self.last_counter:
            gap = now-(self.last_advance or self.started)
            self.max_gap=max(self.max_gap,gap)
            if gap > 30:
                self.failures.append(dict(elapsed=round(now-self.started,1),category='camera_progress_stalled'))
        else:
            self.last_advance=now
        self.last_counter=counter
        for name, value in processes.items():
            key=f'{name}:{value["pid"]}'
            peak=self.peaks.setdefault(key,dict(rss_bytes=0,threads=0))
            peak['rss_bytes']=max(peak['rss_bytes'],value['rss_bytes']); peak['threads']=max(peak['threads'],value['threads'])
            if now-self.started >= 600:
                baseline=self.baselines.setdefault(key,value)
                if value['rss_bytes'] > baseline['rss_bytes']+64*1024*1024 or value['threads'] > baseline['threads']+4:
                    self.failures.append(dict(elapsed=round(now-self.started,1),category='process_resource_growth', role=name))
        if len(self.failures)>10000:
            self.failures=self.failures[:10000]

    def report(self, now, finished=False):
        elapsed=now-self.started
        complete=finished and elapsed >= self.seconds and self.samples >= self.seconds//60
        return dict(schema=1, status='passed' if complete and not self.failures else 'failed' if finished or self.failures else 'running',
            complete=complete, requested_seconds=self.seconds, elapsed_seconds=round(elapsed,1), samples=self.samples,
            max_sampled_progress_gap_seconds=round(self.max_gap,1), failures=self.failures, resource_peaks=self.peaks,
            scope='gateway-observed real-camera frame progress and process resources; not sensor exposure or iPhone display latency')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def run(args):
    parsed=urlsplit(args.gateway)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme not in ['https','http']:
        raise ValueError('explicit gateway origin required')
    if parsed.scheme == 'http' and parsed.hostname not in ['127.0.0.1','::1']:
        raise ValueError('HTTP observation is loopback-only')
    if not 60 <= args.seconds <= 7*86400:
        raise ValueError('bounded 60-second to seven-day observation required')
    token=private_read(args.credential,8192).decode().strip()
    if len(token)!=64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('generated viewer credential required')
    args.output.mkdir(mode=0o700,parents=False,exist_ok=False)
    trust=ssl.create_default_context(cafile=str(args.ca) if args.ca else None)
    opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=trust),NoRedirect())
    catalog_request=urllib.request.Request(args.gateway.rstrip('/')+'/v1/cameras',headers={'Authorization':'Bearer '+token})
    with opener.open(catalog_request,timeout=10) as response:
        catalog=json.loads(response.read(1048577))
    if not any(c.get('camera_id')==args.camera and c.get('source')=='physical' for c in catalog.get('cameras',[])):
        raise ValueError('physical camera enrollment required; synthetic qualification must stay separate')
    begin=time.monotonic(); progress=Progress(args.seconds,begin)
    try:
        while time.monotonic()-begin < args.seconds:
            sample=dict(elapsed_seconds=round(time.monotonic()-begin,1))
            try:
                request=urllib.request.Request(args.gateway.rstrip('/')+'/v1/cameras/'+args.camera+'/activity',
                    headers={'Authorization':'Bearer '+token,'Cache-Control':'no-store'})
                with opener.open(request,timeout=10) as response:
                    data=response.read(1048577)
                    if response.status!=200 or len(data)>1048576:
                        raise ValueError('activity metadata unavailable')
                    page=json.loads(data)
                runtime=json.loads(private_read(args.runtime_state,16384))
                if not {'control','activity'}.issubset(runtime.get('pids',{})) or time.time()-runtime.get('updated_at',0)>10:
                    raise ValueError('owned runtime counters are missing or stale')
                processes={name:dict(pid=pid,**resource(pid)) for name,pid in runtime['pids'].items()}
                progress.sample(time.monotonic(),page,processes)
                sample.update(health=page.get('health'),processes=processes,used_bytes=page['used_bytes'],max_bytes=page['max_bytes'])
            except Exception:
                progress.failures.append(dict(elapsed=round(time.monotonic()-begin,1),category='observation_unavailable'))
                sample['failure']='observation_unavailable'
            with open(args.output/'samples.jsonl','a',opener=lambda p,f:os.open(p,f,0o600)) as file:
                file.write(json.dumps(sample)+'\n')
            atomic(args.output/'summary.json',progress.report(time.monotonic()))
            time.sleep(min(30,max(0,args.seconds-(time.monotonic()-begin))))
    except BaseException:
        progress.failures.append(dict(elapsed=round(time.monotonic()-begin,1),category='observation_interrupted'))
        atomic(args.output/'summary.json',progress.report(time.monotonic(),finished=True))
        raise
    report=progress.report(time.monotonic(),finished=True)
    atomic(args.output/'summary.json',report)
    print('Observation complete:',report['status'])
    return report['status']=='passed'


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gateway',required=True); p.add_argument('--camera',required=True)
    p.add_argument('--credential',required=True,type=Path); p.add_argument('--ca',type=Path)
    p.add_argument('--runtime-state',required=True,type=Path); p.add_argument('--output',required=True,type=Path)
    p.add_argument('--seconds',type=int,default=72*3600)
    a=p.parse_args()
    if not a.camera or len(a.camera)>64 or any(not c.isascii() or not(c.isalnum() or c in '_-') for c in a.camera):
        raise SystemExit('invalid camera identifier')
    raise SystemExit(0 if run(a) else 1)
