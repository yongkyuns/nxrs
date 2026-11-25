#include "includes.h"
#include <pthread.h>
#include <poll.h>
#include <signal.h>
#include <pty.h>
#include <sys/wait.h>
#include <stdatomic.h>
#include <nuttx/sched.h>
#define nitems(a) (sizeof(a)/sizeof((a)[0]))
static int locked, calls, mode, canceled, joined, freed, signals, writes, packets;
static int words[8], word_count;
static int pty_opens;
static void *watched;
static struct task_group_s group={45,100};
static struct tcb_s task={&group};
static pid_t test_getpid(void){return 100;}
static pid_t test_waitpid(pid_t pid,int *status,int options);
static int test_kill(pid_t pid,int signal)
{assert(locked && pid==45 && signal==SIGTERM);signals++;return 0;}
static int test_join(pthread_t thread,void **result)
{(void)thread;(void)result;assert(!freed);return ++joined==1?EINTR:0;}
static int test_poll(struct pollfd *fds,nfds_t count,int timeout)
{assert(!fds && count==0 && timeout==100);return 0;}
static int test_openpty(int *master,int *slave,char *name,const struct termios *term,const struct winsize *size)
{(void)master;(void)slave;(void)name;(void)term;(void)size;pty_opens++;errno=EIO;return -1;}
static ssize_t test_write(int fd,const void *bytes,size_t length)
{
 assert(length==1);
 if(fd==52 || fd==53){assert(*(const unsigned char *)bytes==3);writes++;}
 else assert(fd==51 && *(const unsigned char *)bytes==0);
 return 1;
}
#define getpid test_getpid
#define waitpid test_waitpid
#define kill test_kill
#define pthread_join test_join
#define poll test_poll
#define write test_write
#define openpty test_openpty
#include "netutils/dropbear/dropbear_nshsession.c"
#undef getpid
#undef waitpid
#undef kill
#undef pthread_join
#undef poll
#undef write
#undef openpty
struct sshsession ses;
struct serversession svr_ses;
struct svr_runopts svr_opts;
struct runopts opts;
struct signame signames[]={{"TERM",SIGTERM},{NULL,0}};
static pid_t test_waitpid(pid_t pid,int *status,int options)
{
 assert(pid==45 && options==0 && locked);
 struct dropbear_nshsession_s *s=watched;
 assert(s && !s->reaped && !s->done && s->nsh_pid==45);
 if(calls++==0){errno=mode?ECHILD:EINTR;return -1;}
 *status=7<<8;return pid;
}
int sched_lock(void){locked++;return 0;}
int sched_unlock(void){assert(locked>0);locked--;return 0;}
struct tcb_s *nxsched_get_tcb(pid_t pid){assert(locked && pid==45);return &task;}
void dropbear_service_cancel(void){canceled++;}
void dropbear_service_shell(pid_t pid){assert(pid==-1);}
void dropbear_log(int level,const char *format,...){(void)level;(void)format;}
void *m_malloc(size_t n){return calloc(1,n);}
void m_free_direct(void *p){if(p==watched){assert(joined==2);freed++;}free(p);}
void buf_putbyte(void *p,unsigned char n){assert(p==ses.writepayload);words[word_count++]=n;}
void buf_putint(void *p,unsigned n){assert(p==ses.writepayload);words[word_count++]=(int)n;}
void buf_putstring(void *p,const char *s,unsigned n){assert(p==ses.writepayload && n==11 && strcmp(s,"exit-status")==0);}
void encrypt_packet(void){packets++;}
unsigned buf_getint(void *p){(void)p;abort();}
unsigned char buf_getbool(void *p){(void)p;abort();}
char *buf_getstring(void *p,unsigned *n){(void)p;(void)n;abort();}
struct console_stdio_s *nsh_newconsole(bool value){(void)value;abort();}
int nsh_session(struct console_stdio_s *s,int login,int argc,char **argv)
{(void)s;(void)login;(void)argc;(void)argv;abort();}
void nsh_exit(struct nsh_vtbl_s *v,int result){(void)v;(void)result;abort();}
int posix_spawnattr_setstacksize(posix_spawnattr_t *a,size_t n){(void)a;(void)n;return 0;}
pid_t task_spawn(const char *n,int (*entry)(int,char **),const posix_spawn_file_actions_t *f,const posix_spawnattr_t *a,char *const *v,char *const *e)
{(void)n;(void)entry;(void)f;(void)a;(void)v;(void)e;abort();}
void setnonblocking(int fd){(void)fd;abort();}
void send_msg_channel_success(void *p){(void)p;abort();}
void send_msg_channel_failure(void *p){(void)p;abort();}
int main(void)
{
 for(mode=0;mode<2;mode++)
   {
     struct dropbear_channel channel={.writefd=52,.remotechan=9};
     assert(dropbear_newchansess(&channel)==0);
     struct dropbear_nshsession_s *s=channel.typedata;watched=s;
     s->nsh_pid=45;s->has_pty=true;s->waiter_started=true;
     calls=canceled=joined=freed=signals=writes=packets=word_count=0;
     ses.signal_pipe[1]=51;ses.writepayload=(void *)1;
     /* Cancellation is separate from close protocol and cannot invent an exit. */
     dropbear_nuttx_channel_cancel(&channel);assert(writes==1 && packets==0);
     channel.writefd=-1;channel.readfd=53;
     dropbear_nuttx_channel_cancel(&channel);assert(writes==2 && packets==0);
     assert(dropbear_signal_nsh(s,SIGTERM)==0 && signals==1);
     group.tg_ppid=99;assert(dropbear_signal_nsh(s,SIGTERM)==-ESRCH && signals==1);
     group.tg_ppid=100;
     dropbear_nsh_waiter(s);
     assert(calls==2 && !locked && s->reaped && s->done && s->nsh_pid==-1);
     assert(canceled==mode);
     /* PID retirement does not release the channel's unjoined waiter. */
     assert(dropbear_start_nsh(&channel,s)==DROPBEAR_FAILURE && pty_opens==0);
     assert(s->waiter_started && s->reaped && s->done && joined==0);
     dropbear_closechansess(&channel);
     assert(packets==1 && word_count==4);
     assert(words[0]==SSH_MSG_CHANNEL_REQUEST && words[1]==9 && words[2]==0 && words[3]==7);
     dropbear_closechansess(&channel);assert(packets==1);
     dropbear_cleanupchansess(&channel);
     assert(joined==2 && freed==1 && canceled==mode+1);
   }
 return 0;
}
