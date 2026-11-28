#!/usr/bin/env python3
"""Explicit icount control over an unchanged phase1 corrected ELF."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path('/workspace/scratch/706f9e3bc115/nxrs')
FIXTURE = ROOT / 'tests/nuttx-std/clock-control'
OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('clock_control_workflow', FIXTURE / 'workflow.py')
workflow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = workflow
spec.loader.exec_module(workflow)
oracle = workflow.load_module('clock_control_oracle', FIXTURE / 'oracle.py')
harness = workflow.load_module('clock_control_harness', ROOT / 'tests/nuttx-std/run.py')
ORIGINAL_COMMAND = harness.qemu_command
OPTIONS = ['-icount', 'shift=5,sleep=off']

def controlled_command(*args, **kwargs):
    return ORIGINAL_COMMAND(*args, **kwargs) + OPTIONS

harness.qemu_command = controlled_command

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('variant', choices=['software-corrected', 'software-fine100us'])
args = parser.parse_args()
source_name = {'software-corrected':'software-corrected-retry',
               'software-fine100us':'software-fine100us-retry'}[args.variant]
source = Path('/dev/shm/nxrs-time-sync-phase1') / source_name
out = OUT / args.variant
out.mkdir(exist_ok=False)
build = json.loads((source / 'build.json').read_text())
assert build['build_complete'] is True and build['qemu_clock_correction'] is True
inputs = workflow.sources(True)
assert inputs == build['sources_before'] == build['sources_after']
artifacts = workflow.validate_firmware(source, build['mode'], build['tick_us'])
assert artifacts == build['artifacts']
image = source / build['image']
executable = Path(shutil.which('qemu-system-arm'))
manual = Path('/workspace/scratch/47a087f14644/tools-followup/debian-prefix/usr/share/doc/qemu-system-common/system/invocation.html')
record = {'schema':1, 'variant':args.variant, 'qemu_clock_options':OPTIONS,
          'mode':build['mode'], 'tick_us':build['tick_us'], 'source_build':str(source),
          'build_sha256':workflow.digest(source / 'build.json'), 'artifacts':artifacts,
          'fixture_sources':inputs, 'firmware_identities':build['identities'],
          'experiment_git_head':workflow.git('rev-parse','HEAD'),
          'oracle_sha256':workflow.digest(FIXTURE / 'oracle.py'),
          'harness_sha256':workflow.digest(ROOT / 'tests/nuttx-std/run.py'),
          'driver_sha256':workflow.digest(Path(__file__)),
          'phase1_analysis_sha256':workflow.digest(source / 'analysis.json'),
          'qemu_version':subprocess.check_output([str(executable),'--version'],text=True),
          'qemu_executable':str(executable), 'qemu_sha256':workflow.digest(executable),
          'installed_manual':str(manual), 'installed_manual_sha256':workflow.digest(manual),
          'virtual_ns_per_instruction':32, 'idle_fast_forward_to_timer_deadline':True,
          'cycle_accurate':False, 'physical_clock_qualified':False,
          'characterization_complete':False, 'qualified':False, 'runs':[]}
help_text = subprocess.check_output([str(executable),'--help'],text=True)
help_lines = help_text.splitlines()
start = next(index for index,line in enumerate(help_lines) if line.startswith('-icount '))
(out / 'qemu-icount-help.txt').write_text('\n'.join(help_lines[start:start+5])+'\n')
workflow.save(out / 'analysis.json',record)

def verify(text,status,mode):
    assert re.findall(r'^NXRS_STATUS_([0-9]+)\s*$',status.replace('\r',''),re.M) == ['0']
    record['runs'].append(oracle.analyze(text,mode,build['tick_us']))

started = time.monotonic()
try:
    harness.run_cases(image,out / 'captures',[(build['mode'],'clock_probe')],verify,arm_mps2=True)
    assert workflow.sources(True) == inputs
    assert workflow.validate_firmware(source,build['mode'],build['tick_us']) == artifacts
    assert workflow.digest(FIXTURE / 'oracle.py') == record['oracle_sha256']
    record['characterization_complete'] = True
    record['qualified'] = all(run['qualified'] for run in record['runs'])
except Exception as error:
    record['error'] = str(error)
    raise
finally:
    record['host_elapsed_seconds'] = time.monotonic()-started
    workflow.save(out / 'analysis.json',record)
print(json.dumps({'variant':args.variant,'characterization_complete':record['characterization_complete'],
                  'qualified':record['qualified'],'host_elapsed_seconds':record['host_elapsed_seconds']}))
