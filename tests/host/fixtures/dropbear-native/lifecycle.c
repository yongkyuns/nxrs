#include "includes.h"
#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdatomic.h>
#include <sys/socket.h>
#include <time.h>
static _Thread_local pid_t task_id = 90, parent_id;
static pid_t test_getpid(void) { return task_id; }
static pid_t test_getppid(void) { return parent_id; }
#define getpid test_getpid
#define getppid test_getppid
#include "netutils/dropbear/port/dropbear_control.c"
#define main dropbear_utility_main
#include "netutils/dropbear/dropbear_main.c"
#undef main
#include "netutils/dropbear/dropbear/src/dbmalloc.c"
#undef getpid
#undef getppid
struct sshsession ses;
struct serversession svr_ses;
struct svr_runopts svr_opts;
struct runopts opts;
algo_type sigalgs[]={{"ecdsa",DROPBEAR_SIGNATURE_ECDSA_NISTP256,1},{NULL,0,0}};
void (*_dropbear_exit)(int,const char *,va_list);
void (*_dropbear_log)(int,const char *,va_list);
static atomic_int seed_failure, malformed, store_failure, sessions;
static int retired, setup_calls, owner_return;
void dropbear_exit(const char *format,...)
{ va_list args;va_start(args,format);_dropbear_exit(EXIT_FAILURE,format,args);va_end(args);abort(); }
void dropbear_close(const char *format,...)
{ va_list args;va_start(args,format);_dropbear_exit(EXIT_SUCCESS,format,args);va_end(args);abort(); }
void dropbear_log(int level,const char *format,...){(void)level;(void)format;}
void svr_dropbear_log(int level,const char *format,va_list args)
{(void)level;(void)format;(void)args;}
void svr_getopts(int argc,char **argv)
{
 assert(argc==8 && strcmp(argv[1],"-F")==0 && strcmp(argv[4],"-I")==0 && strcmp(argv[6],"-T")==0);
 setup_calls++;m_malloc(9); /* A real epoch-0 allocation, retired on every failure. */
}
void seedrandom(void){if(atomic_load(&seed_failure))dropbear_exit("entropy");}
void crypto_init(void){}
void dropbear_random_cleanup(void){}
void load_all_hostkeys(void)
{
 assert(sigalgs[0].usable==1);
 if(atomic_load(&malformed)){sigalgs[0].usable=0;dropbear_exit("malformed key");}
 svr_opts.hostkey=m_malloc(31);
}
void sign_key_free(void *key){retired++;m_free(key);}
int dropbear_auth_initialize(void){return atomic_load(&store_failure)?-ENOENT:0;}
int signkey_generate(int kind,int bits,const char *path,int skip)
{(void)kind;(void)bits;(void)path;(void)skip;assert(!"provisioned mode generated a key");return -1;}
void session_cleanup(void)
{if(ses.init_done){m_free(ses.payload);ses.payload=NULL;}}
void svr_session(int fd,int pipefd)
{
 assert(pipefd==-1 && (fcntl(fd,F_GETFD)&FD_CLOEXEC));
 assert(pipe(ses.signal_pipe)==0);
 ses.payload=m_malloc(55);ses.init_done=1;atomic_fetch_add(&sessions,1);
 for(;;)
   {
     struct pollfd p={fd,POLLIN,0};
     if(dropbear_service_stopping())dropbear_close("stop");
     if(poll(&p,1,5)>0)
       {
         char byte;
         if(read(fd,&byte,1)<=0)dropbear_close("peer closed");
       }
   }
}
static void *owner(void *unused)
{
 (void)unused;task_id=7;parent_id=1;
 char *argv[]={"dropbear","start",NULL};owner_return=dropbear_utility_main(2,argv);return NULL;
}
static int connect_server(void)
{
 int fd=socket(AF_INET,SOCK_STREAM,0);assert(fd>=0);
 struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(CONFIG_NETUTILS_DROPBEAR_PORT)};
 assert(inet_pton(AF_INET,"127.0.0.1",&a.sin_addr)==1);
 if(connect(fd,(struct sockaddr *)&a,sizeof(a))<0){close(fd);return -1;}return fd;
}
static int await_connection(void)
{
 for(unsigned i=0;i<500;i++){int fd=connect_server();if(fd>=0)return fd;usleep(1000);}
 assert(!"listener did not become ready");return -1;
}
static void *stopper(void *result)
{task_id=91;parent_id=1;*(int *)result=dropbear_service_stop();return NULL;}
int main(void)
{
 char directory[]="/tmp/dropbear-lifecycle-XXXXXX";assert(mkdtemp(directory));assert(chdir(directory)==0);
 char *start[]={"dropbear",NULL};char *bad[]={"dropbear","-K",NULL};
 assert(dropbear_utility_main(2,bad)==EXIT_FAILURE && setup_calls==0);
 assert(dropbear_utility_main(1,start)==EXIT_FAILURE && staple==NULL);
 assert(connect_server()<0); /* Missing key refuses listener. */
 FILE *key=fopen("hostkey","w");assert(key);fputs("fixture key",key);fclose(key);
 atomic_store(&malformed,1);assert(dropbear_utility_main(1,start)==EXIT_FAILURE && staple==NULL);
 atomic_store(&malformed,0);atomic_store(&seed_failure,1);
 assert(dropbear_utility_main(1,start)==EXIT_FAILURE && staple==NULL);
 atomic_store(&seed_failure,0);atomic_store(&store_failure,1);
 assert(dropbear_utility_main(1,start)==EXIT_FAILURE && staple==NULL);
 atomic_store(&store_failure,0);
 for(unsigned round=0;round<3;round++)
   {
     pthread_t thread;assert(pthread_create(&thread,NULL,owner,NULL)==0);
     int client=await_connection();
     for(unsigned i=0;atomic_load(&sessions)<(int)(round+1)&&i<500;i++)usleep(1000);
     assert(atomic_load(&sessions)==(int)(round+1));
     assert(connect_server()<0); /* Listener closes while transport is owned. */
     assert(dropbear_utility_main(1,start)==EXIT_FAILURE);
     assert(dropbear_service_stop()==0);
     assert(pthread_join(thread,NULL)==0 && owner_return==0 && staple==NULL);
     close(client);
   }
 /* A timeout keeps ownership; a subsequent generation cannot be mistaken for
  * the old instance by a stop already waiting for completion. */
 unsigned generation,next;assert(dropbear_service_begin(&generation)==0);
 assert(dropbear_service_stop()==-EDEADLK);
 dropbear_service_shell(44);task_id=44;assert(dropbear_service_stop()==-EDEADLK);
 task_id=45;parent_id=44;assert(dropbear_service_stop()==-EDEADLK);
 task_id=90;parent_id=0;dropbear_service_shell(-1);
 int result=-999;pthread_t thread;assert(pthread_create(&thread,NULL,stopper,&result)==0);
 assert(pthread_join(thread,NULL)==0 && result==-ETIMEDOUT);
 assert(dropbear_service_begin(&next)==-EBUSY);
 dropbear_service_finish(generation,0);
 assert(dropbear_service_begin(&generation)==0);
 assert(pthread_create(&thread,NULL,stopper,&result)==0);
 while(!dropbear_service_stopping())usleep(1000);
 dropbear_service_finish(generation,0);
 assert(dropbear_service_begin(&next)==0);
 assert(pthread_join(thread,NULL)==0 && result==0);
 assert(!dropbear_service_stopping());dropbear_service_finish(next,0);
 dropbear_service_status();
 assert(retired>=4);unlink("hostkey");assert(chdir("/")==0);assert(rmdir(directory)==0);
 return 0;
}
