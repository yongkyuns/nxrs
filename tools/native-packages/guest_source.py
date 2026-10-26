"""Bounded committed-source snapshot for an owned generated qualification guest.

Git configuration, index, hooks, credentials and untracked build/private state
are never copied. Submodule commit identities are recorded, not their contents.
"""
import hashlib,io,os,re,stat,tarfile
from pathlib import Path,PurePosixPath
import pcre_package as p

MAX_FILES=10000
MAX_FILE=8*1024*1024
MAX_BYTES=64*1024*1024

def require(value,category):p.require(value,category)
def encoded(value):return p.e.encoded(value)
def sha(raw):return hashlib.sha256(raw).hexdigest()

def name(value):
    require(isinstance(value,str) and 0<len(value)<=512 and re.fullmatch(r'[A-Za-z0-9_./+ -]+',value),'public-source-name')
    path=PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix()==value and '..' not in path.parts
            and all(part.lower()!='.git' for part in path.parts),'relative-source-name')
    return path

def target(link,raw):
    require(0<len(raw)<=512 and b'\0' not in raw and b'\n' not in raw,'bounded-source-link')
    text=raw.decode('ascii');destination=PurePosixPath(text)
    require(not destination.is_absolute() and '\\' not in text,'relative-source-link')
    parts=list(name(link).parent.parts)
    for part in destination.parts:
        if part=='..':
            require(bool(parts),'source-link-escapes-root');parts.pop()
        elif part!='.':parts.append(part)
    require(parts and all(v.lower()!='.git' for v in parts),'source-link-target')
    return text

def stable_blob(path,maximum):
    with open(path,'rb',opener=lambda value,flags:os.open(value,flags|os.O_NOFOLLOW|os.O_NONBLOCK)) as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=maximum,'regular-bounded-source-file')
        raw=source.read(maximum+1);after=os.fstat(source.fileno())
        require(len(raw)<=maximum and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'unchanged-source-file')
    return raw

def snapshot(repository,archive,head):
    require(re.fullmatch('[a-f0-9]{40}',head),'exact-source-revision')
    repository=Path(repository).resolve(strict=True);archive=Path(archive)
    def git(*args):return p.u.command(['/usr/bin/git','-C',str(repository),*args])
    require(git('rev-parse','HEAD').strip()==head and not git('status','--porcelain').strip(),'clean-source-snapshot-checkout')
    rows=git('ls-tree','-r','-z','--long',head).split('\0')
    require(1<=len(rows)-1<=MAX_FILES and rows[-1]=='','bounded-source-tree')
    entries={};gitlinks={};total=0
    for row in rows[:-1]:
        header,filename=row.split('\t',1);mode,kind,oid,size=header.split();name(filename)
        require(filename not in entries and filename not in gitlinks and re.fullmatch('[a-f0-9]{40}',oid),'unique-source-entry')
        if mode=='160000':
            require(kind=='commit' and size=='-','source-gitlink-schema');gitlinks[filename]=oid;continue
        require(mode in ('100644','100755','120000') and kind=='blob' and size.isdecimal() and int(size)<=MAX_FILE,'source-blob-schema')
        total+=int(size);require(total<=MAX_BYTES,'source-snapshot-size')
        entries[filename]=dict(mode=mode,git_oid=oid,size=int(size))
    require(entries and not os.path.lexists(archive),'fresh-source-snapshot')
    created=None
    try:
        with open(archive,'xb') as output:
            os.chmod(archive,0o600);created=os.fstat(output.fileno())
            with tarfile.open(fileobj=output,mode='w',format=tarfile.PAX_FORMAT) as result:
                for filename,value in sorted(entries.items()):
                    path=repository/filename
                    if value['mode']=='120000':
                        before=path.lstat();require(stat.S_ISLNK(before.st_mode),'source-link-type')
                        raw=os.fsencode(os.readlink(path));link=target(filename,raw);after=path.lstat()
                        # Lexical bounds alone do not constrain a chain such as
                        # sub/link->../stay and sub/deep/alias->../link/../../outside.
                        require(path.resolve(strict=False).is_relative_to(repository),'resolved-source-link-escapes-root')
                        require((before.st_dev,before.st_ino,before.st_mtime_ns,before.st_ctime_ns)==
                                (after.st_dev,after.st_ino,after.st_mtime_ns,after.st_ctime_ns),'unchanged-source-link')
                    else:raw=stable_blob(path,MAX_FILE)
                    require(len(raw)==value['size'] and hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()==value['git_oid'],'exact-committed-source-blob')
                    value['sha256']=sha(raw)
                    info=tarfile.TarInfo(filename);info.uid=info.gid=info.mtime=0;info.uname=info.gname=''
                    if value['mode']=='120000':
                        info.type=tarfile.SYMTYPE;info.linkname=link;info.mode=0o777;result.addfile(info)
                    else:
                        info.mode=0o755 if value['mode']=='100755' else 0o644;info.size=len(raw);result.addfile(info,io.BytesIO(raw))
            output.flush();os.fsync(output.fileno())
        require(git('rev-parse','HEAD').strip()==head and not git('status','--porcelain').strip(),'unchanged-source-snapshot-checkout')
        manifest=dict(schema=1,scope='committed-guest-source-snapshot',head=head,entries=entries,gitlinks=gitlinks,
            archive_sha256=p.u.file_digest(archive),source_bytes=total,git_configuration_copied=False,gitlink_contents_copied=False)
        verify_archive(archive,manifest);return manifest
    except BaseException:
        if created is not None:
            try:item=archive.lstat()
            except FileNotFoundError:pass
            else:
                if (item.st_dev,item.st_ino,item.st_nlink)==(created.st_dev,created.st_ino,1):archive.unlink()
        raise

def verify_archive(archive,manifest):
    require(manifest['schema']==1 and manifest['scope']=='committed-guest-source-snapshot'
            and manifest['git_configuration_copied'] is False and manifest['gitlink_contents_copied'] is False,'source-snapshot-scope')
    require(p.u.file_digest(archive)==manifest['archive_sha256'],'unchanged-source-archive')
    seen=set();total=0
    with tarfile.open(archive,'r:') as source:
        for item in source:
            name(item.name);require(item.name in manifest['entries'] and item.name not in seen,'declared-source-archive-entry')
            seen.add(item.name);require(len(seen)<=MAX_FILES and item.uid==item.gid==item.mtime==0,'normalized-source-archive-entry')
            value=manifest['entries'][item.name]
            if value['mode']=='120000':
                require(item.issym() and item.size==0,'source-archive-link-type');raw=item.linkname.encode('ascii');target(item.name,raw)
            else:
                require(item.isreg() and item.mode==(0o755 if value['mode']=='100755' else 0o644)
                        and item.size==value['size'] and item.size<=MAX_FILE,'source-archive-regular-file')
                raw=source.extractfile(item).read(MAX_FILE+1)
            total+=len(raw);require(total<=MAX_BYTES and len(raw)==value['size'] and sha(raw)==value['sha256'],'exact-source-archive-data')
    require(seen==set(manifest['entries']) and total==manifest['source_bytes'],'complete-source-archive')

def verify_tree(directory,manifest,owner):
    directory=Path(directory);files=set();folders=set()
    root_metadata=directory.lstat()
    require(stat.S_ISDIR(root_metadata.st_mode) and (root_metadata.st_uid,root_metadata.st_gid)==owner
            and root_metadata.st_mode&0o022==0,'protected-staged-source-root')
    for path in directory.rglob('*'):
        relative=path.relative_to(directory).as_posix();metadata=path.lstat()
        require((metadata.st_uid,metadata.st_gid)==owner,'owned-staged-source')
        if stat.S_ISDIR(metadata.st_mode):
            require(metadata.st_mode&0o022==0,'protected-staged-source-directory');folders.add(relative);continue
        require(relative in manifest['entries'],'no-extra-staged-source')
        value=manifest['entries'][relative];files.add(relative)
        if value['mode']=='120000':
            require(stat.S_ISLNK(metadata.st_mode),'staged-source-link-type');raw=os.fsencode(os.readlink(path));target(relative,raw)
            require(path.resolve(strict=False).is_relative_to(directory.resolve()),'resolved-staged-source-link-escapes-root')
        else:
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_mode&0o777==(0o755 if value['mode']=='100755' else 0o644),'staged-source-mode')
            raw=stable_blob(path,MAX_FILE)
        require(len(raw)==value['size'] and sha(raw)==value['sha256'],'unchanged-staged-source')
    expected_folders={parent.as_posix() for filename in manifest['entries'] for parent in PurePosixPath(filename).parents if parent.as_posix()!='.'}
    require(files==set(manifest['entries']) and folders==expected_folders,'complete-staged-source-tree')

def stage_guest(guest,archive,manifest):
    p.u.hosted() # No privileged/native operation before the hosted guard.
    require(os.getuid()!=0 and archive.parent==guest.work.work and archive.name=='source-snapshot.tar','owned-guest-source-archive')
    guest.verify_unchanged();verify_archive(archive,manifest)
    require(manifest['head']==guest.head,'same-guest-source-revision')
    parent=guest.root/'usr/src';destination=parent/'nxrs'
    require(guest.root.resolve()==guest.root,'canonical-generated-guest-root')
    for path in (guest.root,guest.root/'usr',parent):
        if os.path.lexists(path):
            info=path.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and info.st_mode&0o022==0,'protected-guest-source-parent')
    require(not os.path.lexists(destination),'new-guest-source-directory')
    guest.work.execute(['sudo','-n','install','-d','-m','0755',parent],'guest-source-parent')
    guest.work.execute(['sudo','-n','mkdir','-m','0755',destination],'new-guest-source-directory')
    guest.work.execute(['sudo','-n','/usr/bin/timeout','--signal=KILL','85','/usr/bin/tar','--extract','--file',archive,
        '--directory',destination,'--no-same-owner','--no-same-permissions','--keep-old-files'],'stage-committed-guest-source',90)
    verify_tree(destination,manifest,(0,0));guest.verify_unchanged()
    return destination
