"""GDB-only owned-tracee interruption fixture; never imported by production."""
import json
import os
from pathlib import Path
import sys
import subprocess

def drive(debugger,plan):
    root=Path(plan['store']);target=plan['target'];case=plan['case'];marker=Path(plan['marker'])
    assert case in ('partial-stage','before-selection','after-selection','late-sync-error')
    for setting in ('pagination off','confirm off','disable-randomization off','startup-with-shell off','debuginfod enabled off'):
        debugger.execute('set '+setting)
    debugger.execute('catch syscall fsync',to_string=True)
    output=debugger.execute('run',to_string=True)
    for _ in range(512):
        pid=debugger.selected_inferior().pid
        assert pid>0,'tracee-ended-before-boundary'
        if "call to syscall 'fsync'" in output:
            fd=int(debugger.parse_and_eval('$rdi'))
            path=Path(os.readlink('/proc/'+str(pid)+'/fd/'+str(fd)))
            active=root/'active.json'
            selected=json.loads(active.read_bytes())['approval_sha256'] if active.exists() else None
            stage=path.parent.parent==root/'versions' and path.parent.name.startswith('.stage-') and path.name=='approval.json'
            pending=path==root/'.active.pending'
            published=path==root and selected==target
            hit={'partial-stage':stage,'before-selection':pending,'after-selection':published,'late-sync-error':published}[case]
            if hit:
                value=dict(schema=1,case=case,fsync_entry_observed=True,selected_target=selected==target,
                           stage_members=sorted(p.name for p in path.parent.iterdir()) if stage else [])
                if case=='before-selection':
                    lock=(root/'.install.lock').stat()
                    second=subprocess.run(plan['contender'],capture_output=True,timeout=10)
                    assert second.returncode==1 and b'install-lock' in second.stderr
                    assert (root/'.install.lock').stat().st_ino==lock.st_ino
                    assert json.loads(active.read_bytes())['approval_sha256']==selected
                    value['concurrent_writer_refused']=True
                if case=='late-sync-error':
                    # Linux/amd64 only. Let the owned fsync finish, then
                    # inject -EIO at its syscall-exit stop. This qualifies the
                    # application's uncertain-result handling, not a storage
                    # device failure or power loss. No production test hook.
                    output=debugger.execute('continue',to_string=True)
                    assert "returned from syscall 'fsync'" in output
                    assert int(debugger.parse_and_eval('$rax'))==0
                    value['fsync_completed_before_error_injection']=True
                    debugger.execute('set $rax = -5')
                    debugger.execute('delete breakpoints')
                    debugger.execute('continue',to_string=True)
                    assert debugger.selected_inferior().pid==0
                    value['exit_code']=int(debugger.parse_and_eval('$_exitcode'))
                    assert value['exit_code']==1
                    value['joined']=True
                else:
                    debugger.execute('kill',to_string=True)
                    assert debugger.selected_inferior().pid==0
                    value['joined']=True
                with marker.open('x') as output:json.dump(value,output,sort_keys=True)
                return
        else:
            assert "returned from syscall 'fsync'" in output,'unexpected-tracee-stop'
        output=debugger.execute('continue',to_string=True)
    raise AssertionError('bounded-tracee-stops')

def main():
    # Independently refuse personal-host tracing before reading the plan.
    assert sys.platform=='linux' and os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    plan=json.loads(Path(os.environ['NXRS_PAIR_DEBUG_PLAN']).read_bytes())
    root=Path(plan['store'])
    assert root.parent.parent==Path('/run') and root.parent.name.startswith('nxrs-pair-')
    import gdb
    drive(gdb,plan)

if __name__=='__main__':main()
