#![no_std]
#![feature(rustc_private)]
extern crate libc;
use core::mem::{size_of, size_of_val, align_of, offset_of};
#[used]
#[no_mangle]
#[link_section = ".nxrs_abi"]
pub static NXRS_ABI: [u32; 134] = [
    (size_of::<libc::c_int>()) as u32, // int.size
    (align_of::<libc::c_int>()) as u32, // int.align
    (size_of::<libc::c_long>()) as u32, // long.size
    (align_of::<libc::c_long>()) as u32, // long.align
    (size_of::<libc::size_t>()) as u32, // size_t.size
    (align_of::<libc::size_t>()) as u32, // size_t.align
    (size_of::<libc::time_t>()) as u32, // time_t.size
    (align_of::<libc::time_t>()) as u32, // time_t.align
    (size_of::<libc::off_t>()) as u32, // off_t.size
    (align_of::<libc::off_t>()) as u32, // off_t.align
    (size_of::<libc::nfds_t>()) as u32, // nfds_t.size
    (align_of::<libc::nfds_t>()) as u32, // nfds_t.align
    (size_of::<libc::pthread_t>()) as u32, // pthread_t.size
    (align_of::<libc::pthread_t>()) as u32, // pthread_t.align
    (size_of::<libc::pthread_key_t>()) as u32, // pthread_key_t.size
    (align_of::<libc::pthread_key_t>()) as u32, // pthread_key_t.align
    (size_of::<libc::ino_t>()) as u32, // ino_t.size
    (align_of::<libc::ino_t>()) as u32, // ino_t.align
    (size_of::<libc::blksize_t>()) as u32, // blksize_t.size
    (align_of::<libc::blksize_t>()) as u32, // blksize_t.align
    (size_of::<libc::wchar_t>()) as u32, // wchar_t.size
    (align_of::<libc::wchar_t>()) as u32, // wchar_t.align
    (size_of::<libc::pthread_attr_t>()) as u32, // pthread_attr_t.size
    (align_of::<libc::pthread_attr_t>()) as u32, // pthread_attr_t.align
    (size_of::<libc::pthread_mutex_t>()) as u32, // pthread_mutex_t.size
    (align_of::<libc::pthread_mutex_t>()) as u32, // pthread_mutex_t.align
    (size_of::<libc::pthread_mutexattr_t>()) as u32, // pthread_mutexattr_t.size
    (align_of::<libc::pthread_mutexattr_t>()) as u32, // pthread_mutexattr_t.align
    (size_of::<libc::pthread_cond_t>()) as u32, // pthread_cond_t.size
    (align_of::<libc::pthread_cond_t>()) as u32, // pthread_cond_t.align
    (size_of::<libc::pthread_condattr_t>()) as u32, // pthread_condattr_t.size
    (align_of::<libc::pthread_condattr_t>()) as u32, // pthread_condattr_t.align
    (size_of::<libc::pthread_rwlock_t>()) as u32, // pthread_rwlock_t.size
    (align_of::<libc::pthread_rwlock_t>()) as u32, // pthread_rwlock_t.align
    (size_of::<libc::pthread_rwlockattr_t>()) as u32, // pthread_rwlockattr_t.size
    (align_of::<libc::pthread_rwlockattr_t>()) as u32, // pthread_rwlockattr_t.align
    (size_of::<libc::sem_t>()) as u32, // sem_t.size
    (align_of::<libc::sem_t>()) as u32, // sem_t.align
    (size_of::<libc::timespec>()) as u32, // timespec.size
    (align_of::<libc::timespec>()) as u32, // timespec.align
    (size_of::<libc::pollfd>()) as u32, // pollfd.size
    (align_of::<libc::pollfd>()) as u32, // pollfd.align
    (size_of::<libc::dirent>()) as u32, // dirent.size
    (align_of::<libc::dirent>()) as u32, // dirent.align
    (size_of::<libc::stat>()) as u32, // stat.size
    (align_of::<libc::stat>()) as u32, // stat.align
    (size_of::<libc::pollevent_t>()) as u32, // pollevent_t.size
    (align_of::<libc::pollevent_t>()) as u32, // pollevent_t.align
    (size_of::<libc::pollcb_t>()) as u32, // pollcb_t.size
    (align_of::<libc::pollcb_t>()) as u32, // pollcb_t.align
    (offset_of!(libc::dirent, d_ino)) as u32, // dirent.d_ino
    (size_of_val(&unsafe { core::mem::zeroed::<libc::dirent>() }.d_ino)) as u32, // dirent.d_ino.size
    (offset_of!(libc::dirent, d_type)) as u32, // dirent.d_type
    (size_of_val(&unsafe { core::mem::zeroed::<libc::dirent>() }.d_type)) as u32, // dirent.d_type.size
    (offset_of!(libc::dirent, d_name)) as u32, // dirent.d_name
    (size_of_val(&unsafe { core::mem::zeroed::<libc::dirent>() }.d_name)) as u32, // dirent.d_name.size
    (offset_of!(libc::stat, st_dev)) as u32, // stat.st_dev
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_dev)) as u32, // stat.st_dev.size
    (offset_of!(libc::stat, st_ino)) as u32, // stat.st_ino
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_ino)) as u32, // stat.st_ino.size
    (offset_of!(libc::stat, st_mode)) as u32, // stat.st_mode
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_mode)) as u32, // stat.st_mode.size
    (offset_of!(libc::stat, st_nlink)) as u32, // stat.st_nlink
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_nlink)) as u32, // stat.st_nlink.size
    (offset_of!(libc::stat, st_uid)) as u32, // stat.st_uid
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_uid)) as u32, // stat.st_uid.size
    (offset_of!(libc::stat, st_gid)) as u32, // stat.st_gid
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_gid)) as u32, // stat.st_gid.size
    (offset_of!(libc::stat, st_rdev)) as u32, // stat.st_rdev
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_rdev)) as u32, // stat.st_rdev.size
    (offset_of!(libc::stat, st_size)) as u32, // stat.st_size
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_size)) as u32, // stat.st_size.size
    (offset_of!(libc::stat, st_atim)) as u32, // stat.st_atim
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_atim)) as u32, // stat.st_atim.size
    (offset_of!(libc::stat, st_mtim)) as u32, // stat.st_mtim
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_mtim)) as u32, // stat.st_mtim.size
    (offset_of!(libc::stat, st_ctim)) as u32, // stat.st_ctim
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_ctim)) as u32, // stat.st_ctim.size
    (offset_of!(libc::stat, st_blksize)) as u32, // stat.st_blksize
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_blksize)) as u32, // stat.st_blksize.size
    (offset_of!(libc::stat, st_blocks)) as u32, // stat.st_blocks
    (size_of_val(&unsafe { core::mem::zeroed::<libc::stat>() }.st_blocks)) as u32, // stat.st_blocks.size
    (offset_of!(libc::timespec, tv_sec)) as u32, // timespec.tv_sec
    (offset_of!(libc::timespec, tv_nsec)) as u32, // timespec.tv_nsec
    (offset_of!(libc::pollfd, fd)) as u32, // pollfd.fd
    (offset_of!(libc::pollfd, events)) as u32, // pollfd.events
    (offset_of!(libc::pollfd, revents)) as u32, // pollfd.revents
    (offset_of!(libc::pollfd, arg)) as u32, // pollfd.arg
    (offset_of!(libc::pollfd, cb)) as u32, // pollfd.cb
    (offset_of!(libc::pollfd, priv_)) as u32, // pollfd.priv
    (size_of_val(&unsafe { core::mem::zeroed::<libc::pollfd>() }.events)) as u32, // pollfd.events.size
    (size_of_val(&unsafe { core::mem::zeroed::<libc::pollfd>() }.revents)) as u32, // pollfd.revents.size
    (libc::CLOCK_MONOTONIC) as u32, // CLOCK_MONOTONIC
    (libc::CLOCK_REALTIME) as u32, // CLOCK_REALTIME
    (libc::ETIMEDOUT) as u32, // ETIMEDOUT
    (libc::EINTR) as u32, // EINTR
    (libc::EBADF) as u32, // EBADF
    (libc::F_GETFD) as u32, // F_GETFD
    (libc::PTHREAD_MUTEX_NORMAL) as u32, // PTHREAD_MUTEX_NORMAL
    (libc::SIGPIPE) as u32, // SIGPIPE
    (libc::O_RDONLY) as u32, // O_RDONLY
    (libc::O_WRONLY) as u32, // O_WRONLY
    (libc::O_RDWR) as u32, // O_RDWR
    (libc::O_CREAT) as u32, // O_CREAT
    (libc::O_EXCL) as u32, // O_EXCL
    (libc::O_NOCTTY) as u32, // O_NOCTTY
    (libc::O_TRUNC) as u32, // O_TRUNC
    (libc::O_APPEND) as u32, // O_APPEND
    (libc::O_NONBLOCK) as u32, // O_NONBLOCK
    (libc::O_DSYNC) as u32, // O_DSYNC
    (libc::O_DIRECT) as u32, // O_DIRECT
    (libc::O_LARGEFILE) as u32, // O_LARGEFILE
    (libc::O_DIRECTORY) as u32, // O_DIRECTORY
    (libc::O_NOFOLLOW) as u32, // O_NOFOLLOW
    (libc::O_NOATIME) as u32, // O_NOATIME
    (libc::O_CLOEXEC) as u32, // O_CLOEXEC
    (libc::O_ACCMODE) as u32, // O_ACCMODE
    (libc::SOL_SOCKET) as u32, // SOL_SOCKET
    (libc::SO_BROADCAST) as u32, // SO_BROADCAST
    (libc::POLLIN) as u32, // POLLIN
    (libc::POLLRDNORM) as u32, // POLLRDNORM
    (libc::POLLRDBAND) as u32, // POLLRDBAND
    (libc::POLLPRI) as u32, // POLLPRI
    (libc::POLLOUT) as u32, // POLLOUT
    (libc::POLLWRNORM) as u32, // POLLWRNORM
    (libc::POLLWRBAND) as u32, // POLLWRBAND
    (libc::POLLERR) as u32, // POLLERR
    (libc::POLLHUP) as u32, // POLLHUP
    (libc::POLLRDHUP) as u32, // POLLRDHUP
    (libc::POLLNVAL) as u32, // POLLNVAL
    (0) as u32, // SEM_PRIO_NONE
    (libc::SIG_IGN) as u32, // SIG_IGN
    (libc::SIG_DFL) as u32, // SIG_DFL
    (libc::SIG_ERR) as u32, // SIG_ERR
];
