/* Owned disposable-process controls only. Never print canary bytes or paths. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/sched.h>
#include <sys/wait.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>
#include <signal.h>
#if defined(__x86_64__)
_Static_assert(AUDIT_ARCH_X86_64 == 0xc000003e, "native audit architecture");
#elif defined(__aarch64__)
_Static_assert(AUDIT_ARCH_AARCH64 == 0xc00000b7, "native audit architecture");
#else
#error "Unqualified audit architecture"
#endif
_Static_assert(sizeof(struct sock_fprog) == 16 && offsetof(struct sock_fprog,filter) == 8, "native BPF program ABI");
_Static_assert(sizeof(struct sock_filter) == 8, "native BPF ABI");
_Static_assert(offsetof(struct seccomp_data, arch) == 4 && offsetof(struct seccomp_data, nr) == 0, "native seccomp offsets");
_Static_assert(offsetof(struct seccomp_data, args) == 16, "native seccomp argument offset");
_Static_assert(CLONE_THREAD == 0x10000, "native thread group flag");
static int joined(pid_t pid) {
 if(pid==0)_exit(0);
 if(pid<0)return 0;
 int status;return waitpid(pid,&status,0)==pid && WIFEXITED(status) && WEXITSTATUS(status)==0;
}
static int clone_ok(void) { return joined((pid_t)syscall(SYS_clone,(unsigned long)SIGCHLD,NULL,NULL,NULL,NULL)); }
static int clone_new_ok(void) { struct clone_args args={0};args.exit_signal=SIGCHLD;return joined((pid_t)syscall(SYS_clone3,&args,sizeof args)); }
static int readable(const char *path) { char b[32]; int f=open(path,O_RDONLY); if(f<0)return 0; int result=read(f,b,sizeof b)==sizeof b; close(f); return result; }
static int socket_ok(int family,int type) { int f=socket(family,type,0); if(f<0)return 0; close(f); return 1; }
static int create(const char *path) { int f=open(path,O_CREAT|O_WRONLY|O_EXCL,0600); if(f<0)return 0; close(f); return 1; }
int main(int argc,char **argv) {
 if(argc!=9)return 2;
 char gate;if(read(0,&gate,1)!=1||gate!='g')return 3;
 pid_t parent=(pid_t)strtol(argv[6],NULL,10);
 char memory[32];struct iovec local={memory,sizeof memory}, remote={(void*)(uintptr_t)strtoull(argv[7],NULL,16),sizeof memory};
 int parent_memory=process_vm_readv(parent,&local,1,&remote,1,0)==sizeof memory && memcmp(memory,"qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",32)==0;
 int inherited=fcntl((int)strtol(argv[8],NULL,10),F_GETFD)>=0;
 printf("secret=%d symlink=%d outside_write=%d work_write=%d parent_memory=%d inherited=%d tcp=%d udp=%d unix=%d signal=%d hardlink=%d fork=%d clone=%d clone_new=%d\n",
 readable(argv[1]),readable(argv[2]),create(argv[3]),create(argv[4]),parent_memory,inherited,
 socket_ok(AF_INET,SOCK_STREAM),socket_ok(AF_INET,SOCK_DGRAM),socket_ok(AF_UNIX,SOCK_STREAM),
 kill(parent,0)==0,link(argv[1],argv[5])==0,joined(fork()),clone_ok(),clone_new_ok());
 return 0;
}
