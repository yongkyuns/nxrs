"""Read-only systemd/cgroup lifetime checks for the two owned hosted fixtures."""
from pathlib import Path,PurePosixPath

PROPERTIES=('LoadState','ActiveState','SubState','Result','MainPID','ControlPID','Job',
            'ControlGroup','ExecMainStartTimestampMonotonic')
PROPERTY_ARGS=tuple(part for key in PROPERTIES for part in ('-p',key))


def parse(raw):
    if isinstance(raw,bytes):raw=raw.decode('ascii')
    if len(raw)>8192:raise ValueError('bounded-service-state')
    result={}
    for line in raw.splitlines():
        key,separator,value=line.partition('=')
        if not separator or key not in PROPERTIES or key in result:raise ValueError('service-state-schema')
        result[key]=value
    if set(result)!=set(PROPERTIES):raise ValueError('complete-service-state')
    for key in ['MainPID','ControlPID','ExecMainStartTimestampMonotonic']:
        if not result[key].isdigit():raise ValueError('numeric-service-state')
    return result


def quiescent(state,unit,*,allow_latch=False,cgroup_root=Path('/sys/fs/cgroup')):
    if state['LoadState']!='loaded' or state['MainPID']!='0' or state['ControlPID']!='0':return False
    if state['Job'] not in ('','0'):return False
    terminal=state['ActiveState'] in ('inactive','failed')
    latched=allow_latch and state['ActiveState']=='active' and state['SubState']=='exited'
    if not (terminal or latched):return False
    group=state['ControlGroup']
    if not group:return True
    path=PurePosixPath(group)
    if not path.is_absolute() or '..' in path.parts or path.as_posix()!=group or path.name!=unit:return False
    folder=cgroup_root/path.relative_to('/')
    try:
        if not folder.exists():return True
        files=list(folder.rglob('cgroup.procs'))
        if not files or len(files)>256:return False
        # Do not infer child retirement from MainPID, or overlook nested cgroups.
        return all(not p.read_text().strip() for p in files)
    except OSError:return False


def handler_finished(state,unit,previous,**kwargs):
    return (int(state['ExecMainStartTimestampMonotonic'])>previous and state['Result']=='success'
        and state['ActiveState']=='active' and state['SubState']=='exited'
        and quiescent(state,unit,allow_latch=True,**kwargs))
