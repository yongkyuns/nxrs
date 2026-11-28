#include <nuttx/config.h>
#include <stdint.h>
#include <stddef.h>
#include <time.h>
#include <sys/types.h>
#include <poll.h>
#include <pthread.h>
#include <semaphore.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/socket.h>
__attribute__((section(".nxrs_abi"), used))
const uint32_t nxrs_abi[] = {
  (uint32_t)(sizeof(int)), /* int.size */
  (uint32_t)(_Alignof(int)), /* int.align */
  (uint32_t)(sizeof(long)), /* long.size */
  (uint32_t)(_Alignof(long)), /* long.align */
  (uint32_t)(sizeof(size_t)), /* size_t.size */
  (uint32_t)(_Alignof(size_t)), /* size_t.align */
  (uint32_t)(sizeof(time_t)), /* time_t.size */
  (uint32_t)(_Alignof(time_t)), /* time_t.align */
  (uint32_t)(sizeof(off_t)), /* off_t.size */
  (uint32_t)(_Alignof(off_t)), /* off_t.align */
  (uint32_t)(sizeof(nfds_t)), /* nfds_t.size */
  (uint32_t)(_Alignof(nfds_t)), /* nfds_t.align */
  (uint32_t)(sizeof(pthread_t)), /* pthread_t.size */
  (uint32_t)(_Alignof(pthread_t)), /* pthread_t.align */
  (uint32_t)(sizeof(pthread_key_t)), /* pthread_key_t.size */
  (uint32_t)(_Alignof(pthread_key_t)), /* pthread_key_t.align */
  (uint32_t)(sizeof(ino_t)), /* ino_t.size */
  (uint32_t)(_Alignof(ino_t)), /* ino_t.align */
  (uint32_t)(sizeof(blksize_t)), /* blksize_t.size */
  (uint32_t)(_Alignof(blksize_t)), /* blksize_t.align */
  (uint32_t)(sizeof(wchar_t)), /* wchar_t.size */
  (uint32_t)(_Alignof(wchar_t)), /* wchar_t.align */
  (uint32_t)(sizeof(pthread_attr_t)), /* pthread_attr_t.size */
  (uint32_t)(_Alignof(pthread_attr_t)), /* pthread_attr_t.align */
  (uint32_t)(sizeof(pthread_mutex_t)), /* pthread_mutex_t.size */
  (uint32_t)(_Alignof(pthread_mutex_t)), /* pthread_mutex_t.align */
  (uint32_t)(sizeof(pthread_mutexattr_t)), /* pthread_mutexattr_t.size */
  (uint32_t)(_Alignof(pthread_mutexattr_t)), /* pthread_mutexattr_t.align */
  (uint32_t)(sizeof(pthread_cond_t)), /* pthread_cond_t.size */
  (uint32_t)(_Alignof(pthread_cond_t)), /* pthread_cond_t.align */
  (uint32_t)(sizeof(pthread_condattr_t)), /* pthread_condattr_t.size */
  (uint32_t)(_Alignof(pthread_condattr_t)), /* pthread_condattr_t.align */
  (uint32_t)(sizeof(pthread_rwlock_t)), /* pthread_rwlock_t.size */
  (uint32_t)(_Alignof(pthread_rwlock_t)), /* pthread_rwlock_t.align */
  (uint32_t)(sizeof(pthread_rwlockattr_t)), /* pthread_rwlockattr_t.size */
  (uint32_t)(_Alignof(pthread_rwlockattr_t)), /* pthread_rwlockattr_t.align */
  (uint32_t)(sizeof(sem_t)), /* sem_t.size */
  (uint32_t)(_Alignof(sem_t)), /* sem_t.align */
  (uint32_t)(sizeof(struct timespec)), /* timespec.size */
  (uint32_t)(_Alignof(struct timespec)), /* timespec.align */
  (uint32_t)(sizeof(struct pollfd)), /* pollfd.size */
  (uint32_t)(_Alignof(struct pollfd)), /* pollfd.align */
  (uint32_t)(sizeof(struct dirent)), /* dirent.size */
  (uint32_t)(_Alignof(struct dirent)), /* dirent.align */
  (uint32_t)(sizeof(struct stat)), /* stat.size */
  (uint32_t)(_Alignof(struct stat)), /* stat.align */
  (uint32_t)(sizeof(pollevent_t)), /* pollevent_t.size */
  (uint32_t)(_Alignof(pollevent_t)), /* pollevent_t.align */
  (uint32_t)(sizeof(pollcb_t)), /* pollcb_t.size */
  (uint32_t)(_Alignof(pollcb_t)), /* pollcb_t.align */
  (uint32_t)(offsetof(struct dirent, d_ino)), /* dirent.d_ino */
  (uint32_t)(sizeof(((struct dirent *)0)->d_ino)), /* dirent.d_ino.size */
  (uint32_t)(offsetof(struct dirent, d_type)), /* dirent.d_type */
  (uint32_t)(sizeof(((struct dirent *)0)->d_type)), /* dirent.d_type.size */
  (uint32_t)(offsetof(struct dirent, d_name)), /* dirent.d_name */
  (uint32_t)(sizeof(((struct dirent *)0)->d_name)), /* dirent.d_name.size */
  (uint32_t)(offsetof(struct stat, st_dev)), /* stat.st_dev */
  (uint32_t)(sizeof(((struct stat *)0)->st_dev)), /* stat.st_dev.size */
  (uint32_t)(offsetof(struct stat, st_ino)), /* stat.st_ino */
  (uint32_t)(sizeof(((struct stat *)0)->st_ino)), /* stat.st_ino.size */
  (uint32_t)(offsetof(struct stat, st_mode)), /* stat.st_mode */
  (uint32_t)(sizeof(((struct stat *)0)->st_mode)), /* stat.st_mode.size */
  (uint32_t)(offsetof(struct stat, st_nlink)), /* stat.st_nlink */
  (uint32_t)(sizeof(((struct stat *)0)->st_nlink)), /* stat.st_nlink.size */
  (uint32_t)(offsetof(struct stat, st_uid)), /* stat.st_uid */
  (uint32_t)(sizeof(((struct stat *)0)->st_uid)), /* stat.st_uid.size */
  (uint32_t)(offsetof(struct stat, st_gid)), /* stat.st_gid */
  (uint32_t)(sizeof(((struct stat *)0)->st_gid)), /* stat.st_gid.size */
  (uint32_t)(offsetof(struct stat, st_rdev)), /* stat.st_rdev */
  (uint32_t)(sizeof(((struct stat *)0)->st_rdev)), /* stat.st_rdev.size */
  (uint32_t)(offsetof(struct stat, st_size)), /* stat.st_size */
  (uint32_t)(sizeof(((struct stat *)0)->st_size)), /* stat.st_size.size */
  (uint32_t)(offsetof(struct stat, st_atim)), /* stat.st_atim */
  (uint32_t)(sizeof(((struct stat *)0)->st_atim)), /* stat.st_atim.size */
  (uint32_t)(offsetof(struct stat, st_mtim)), /* stat.st_mtim */
  (uint32_t)(sizeof(((struct stat *)0)->st_mtim)), /* stat.st_mtim.size */
  (uint32_t)(offsetof(struct stat, st_ctim)), /* stat.st_ctim */
  (uint32_t)(sizeof(((struct stat *)0)->st_ctim)), /* stat.st_ctim.size */
  (uint32_t)(offsetof(struct stat, st_blksize)), /* stat.st_blksize */
  (uint32_t)(sizeof(((struct stat *)0)->st_blksize)), /* stat.st_blksize.size */
  (uint32_t)(offsetof(struct stat, st_blocks)), /* stat.st_blocks */
  (uint32_t)(sizeof(((struct stat *)0)->st_blocks)), /* stat.st_blocks.size */
  (uint32_t)(offsetof(struct timespec, tv_sec)), /* timespec.tv_sec */
  (uint32_t)(offsetof(struct timespec, tv_nsec)), /* timespec.tv_nsec */
  (uint32_t)(offsetof(struct pollfd, fd)), /* pollfd.fd */
  (uint32_t)(offsetof(struct pollfd, events)), /* pollfd.events */
  (uint32_t)(offsetof(struct pollfd, revents)), /* pollfd.revents */
  (uint32_t)(offsetof(struct pollfd, arg)), /* pollfd.arg */
  (uint32_t)(offsetof(struct pollfd, cb)), /* pollfd.cb */
  (uint32_t)(offsetof(struct pollfd, priv)), /* pollfd.priv */
  (uint32_t)(sizeof(((struct pollfd *)0)->events)), /* pollfd.events.size */
  (uint32_t)(sizeof(((struct pollfd *)0)->revents)), /* pollfd.revents.size */
  (uint32_t)(CLOCK_MONOTONIC), /* CLOCK_MONOTONIC */
  (uint32_t)(CLOCK_REALTIME), /* CLOCK_REALTIME */
  (uint32_t)(ETIMEDOUT), /* ETIMEDOUT */
  (uint32_t)(EINTR), /* EINTR */
  (uint32_t)(EBADF), /* EBADF */
  (uint32_t)(F_GETFD), /* F_GETFD */
  (uint32_t)(PTHREAD_MUTEX_NORMAL), /* PTHREAD_MUTEX_NORMAL */
  (uint32_t)(SIGPIPE), /* SIGPIPE */
  (uint32_t)(O_RDONLY), /* O_RDONLY */
  (uint32_t)(O_WRONLY), /* O_WRONLY */
  (uint32_t)(O_RDWR), /* O_RDWR */
  (uint32_t)(O_CREAT), /* O_CREAT */
  (uint32_t)(O_EXCL), /* O_EXCL */
  (uint32_t)(O_NOCTTY), /* O_NOCTTY */
  (uint32_t)(O_TRUNC), /* O_TRUNC */
  (uint32_t)(O_APPEND), /* O_APPEND */
  (uint32_t)(O_NONBLOCK), /* O_NONBLOCK */
  (uint32_t)(O_DSYNC), /* O_DSYNC */
  (uint32_t)(O_DIRECT), /* O_DIRECT */
  (uint32_t)(O_LARGEFILE), /* O_LARGEFILE */
  (uint32_t)(O_DIRECTORY), /* O_DIRECTORY */
  (uint32_t)(O_NOFOLLOW), /* O_NOFOLLOW */
  (uint32_t)(O_NOATIME), /* O_NOATIME */
  (uint32_t)(O_CLOEXEC), /* O_CLOEXEC */
  (uint32_t)(O_ACCMODE), /* O_ACCMODE */
  (uint32_t)(SOL_SOCKET), /* SOL_SOCKET */
  (uint32_t)(SO_BROADCAST), /* SO_BROADCAST */
  (uint32_t)(POLLIN), /* POLLIN */
  (uint32_t)(POLLRDNORM), /* POLLRDNORM */
  (uint32_t)(POLLRDBAND), /* POLLRDBAND */
  (uint32_t)(POLLPRI), /* POLLPRI */
  (uint32_t)(POLLOUT), /* POLLOUT */
  (uint32_t)(POLLWRNORM), /* POLLWRNORM */
  (uint32_t)(POLLWRBAND), /* POLLWRBAND */
  (uint32_t)(POLLERR), /* POLLERR */
  (uint32_t)(POLLHUP), /* POLLHUP */
  (uint32_t)(POLLRDHUP), /* POLLRDHUP */
  (uint32_t)(POLLNVAL), /* POLLNVAL */
  (uint32_t)(SEM_PRIO_NONE), /* SEM_PRIO_NONE */
  (uint32_t)((uintptr_t)SIG_IGN), /* SIG_IGN */
  (uint32_t)((uintptr_t)SIG_DFL), /* SIG_DFL */
  (uint32_t)((uintptr_t)SIG_ERR), /* SIG_ERR */
};
