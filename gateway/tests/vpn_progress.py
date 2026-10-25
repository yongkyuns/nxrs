"""Bounded private progress witness for the owned VPN namespace receiver."""
import asyncio
import json
import os
import stat
from pathlib import Path
import time


def require(value,category):
    if not value:raise AssertionError(category)


def publish(path,frames,ended=False):
    path=Path(path);pending=path.with_name(path.name+'.pending')
    raw=(json.dumps(dict(schema=1,frames=frames,ended=ended),separators=(',',':'))+'\n').encode()
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as output:output.write(raw)
        os.replace(pending,path)
    finally:
        try:pending.unlink()
        except FileNotFoundError:pass


def read_progress(path):
    # The writer atomically replaces complete immutable snapshots. An open old
    # inode is valid even if its name has just advanced to the following snapshot.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid==os.geteuid() and before.st_mode&0o077==0
            and 0<before.st_size<=1024,'private bounded progress snapshot required')
        raw=source.read(1025);after=os.fstat(source.fileno())
        identity=lambda m:(m.st_dev,m.st_ino,m.st_mode,m.st_uid,m.st_gid,m.st_size,m.st_mtime_ns)
        require(len(raw)<=1024 and identity(before)==identity(after),'complete progress snapshot required')
    value=json.loads(raw)
    require(isinstance(value,dict) and set(value)=={'schema','frames','ended'} and type(value['schema']) is int and value['schema']==1
        and type(value['frames']) is int and 5<=value['frames']<=10000 and type(value['ended']) is bool,'bounded progress schema required')
    return value


async def hold_frames(receiver,progress,stop,initial,last_pts,*,seconds=90,poll=.25,end_errors=()):
    """Own and join exactly one pending receive, including network silence."""
    progress=Path(progress);stop=Path(stop)
    require(progress.is_absolute() and stop.is_absolute() and progress.parent==stop.parent
        and not os.path.lexists(progress) and not os.path.lexists(stop),'new private progress fixtures required')
    require(0<seconds<=90 and 0<poll<=.25,'bounded progress witness required')
    require(type(initial) is int and initial>=5 and type(last_pts) is int,'decoded baseline required')
    frames=initial;deadline=time.monotonic()+seconds
    publish(progress,frames)
    pending=asyncio.create_task(receiver.recv())
    try:
        while not stop.exists():
            remaining=deadline-time.monotonic()
            require(remaining>0,'active VPN receiver witness deadline')
            if pending is None:
                await asyncio.sleep(min(poll,remaining));continue
            try:frame=await asyncio.wait_for(asyncio.shield(pending),min(poll,remaining))
            except asyncio.TimeoutError:continue
            except end_errors:
                pending=None;publish(progress,frames,ended=True);continue
            require((frame.width,frame.height)==(320,240),'active VPN decoded dimensions')
            require(type(frame.pts) is int and frame.pts>last_pts,'active VPN advancing decoded image')
            last_pts=frame.pts
            frames+=1;require(frames<=10000,'active VPN decoded frame budget')
            publish(progress,frames)
            pending=asyncio.create_task(receiver.recv())
        return frames
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)
