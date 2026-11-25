#pragma once
#include <nuttx/config.h>
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
#define noreturn_function __attribute__((noreturn))
#define DROPBEAR_NUTTX 1
#define DROPBEAR_NUTTX_PASSWD 1
#define DROPBEAR_SVR_PASSWORD_AUTH 1
#define DROPBEAR_TRACKING_MALLOC 1
#define DROPBEAR_FUZZ 0
#define DROPBEAR_USE_PRNGD 0
#define SHA256_HASH_SIZE 32
#define DROPBEAR_SUCCESS 0
#define DROPBEAR_FAILURE -1
#define DROPBEAR_MAX_PASSWORD_LEN 256
#define LOG_ERR 3
#define LOG_WARNING 4
#define LOG_INFO 6
#define LOG_NOTICE 5
#define SIZE_T_MAX SIZE_MAX
#define UNUSED(x) (void)(x)
#define TRACE(x)
#define TRACE2(x)
#define DEBUG1(x)
#define CHECKCLEARTOWRITE() ((void)0)
#ifndef MIN
#define MIN(a,b) ((a)<(b)?(a):(b))
#endif
#ifndef MAX
#define MAX(a,b) ((a)>(b)?(a):(b))
#endif
#define static_assert _Static_assert
#define LTC_ARGCHK(x) do { if (!(x)) return CRYPT_ERROR; } while (0)
#define zeromem(p,n) memset((p),0,(n))
#define CRYPT_OK 0
#define CRYPT_ERROR -1
#define CRYPT_INVALID_HASH -2
#define CRYPT_INVALID_KEYSIZE -3
#define CRYPT_OVERFLOW -4
#define AUTH_TYPE_PASSWORD 1
#define SSH_MSG_CHANNEL_REQUEST 98
#define SSH_OPEN_RESOURCE_SHORTAGE 4
#define DROPBEAR_PRIO_LOWDELAY 0
#define DROPBEAR_PRIO_NORMAL 0
#define FD_UNINIT -2
#define FD_CLOSED -1
#define CHAN_EXTEND_SIZE 4
#define MAX_CHANNELS 1000
#define RECV_MAX_CHANNEL_DATA_LEN 32759
#define Channel dropbear_channel
#define ChanType dropbear_chantype
struct dropbear_channel;
struct dropbear_chantype {
 const char *name;
 int (*init)(struct dropbear_channel *);
 int (*check_close)(struct dropbear_channel *);
 void (*request)(struct dropbear_channel *);
 void (*closehandler)(const struct dropbear_channel *);
 void (*cleanup)(const struct dropbear_channel *);
};
struct dropbear_channel {
 void *typedata; int prio, readfd, writefd, errfd, bidir_fd;
 unsigned remotechan,index,sent_close,recv_close,sent_eof,recv_eof;
 unsigned transwindow,transmaxpacket,await_open,recvwindow,recvdonelen,recvmaxpacket;
 void *writebuf,*extrabuf,*conn_pending; const struct dropbear_chantype *type;
};
extern const struct dropbear_chantype svrchansess;
struct sshsession {
 int sock_in,sock_out,signal_pipe[2],init_done,maxfd;
 void *payload,*writepayload;
 struct { int authdone; char *pw_name; unsigned failcount,authtypes; } authstate;
 struct dropbear_channel **channels; unsigned chansize,chancount;
};
struct serversession { char *addrstring; };
struct svr_runopts { void *hostkey; int multiauthmethod; unsigned int maxauthtries; };
struct runopts { unsigned recv_window; };
extern struct sshsession ses;
extern struct serversession svr_ses;
extern struct svr_runopts svr_opts;
extern struct runopts opts;
typedef struct { const char *name; int val,usable; } algo_type;
extern algo_type sigalgs[];
#define DROPBEAR_SIGNATURE_ECDSA_NISTP256 1
#define DROPBEAR_SIGNKEY_ECDSA_NISTP256 1
extern void (*_dropbear_exit)(int,const char *,va_list);
extern void (*_dropbear_log)(int,const char *,va_list);
void dropbear_exit(const char *,...) __attribute__((noreturn));
void dropbear_close(const char *,...) __attribute__((noreturn));
void dropbear_log(int,const char *,...);
void svr_dropbear_log(int,const char *,va_list);
void svr_getopts(int,char **);
void seedrandom(void);
void crypto_init(void);
void load_all_hostkeys(void);
void sign_key_free(void *);
void svr_session(int,int);
void session_cleanup(void);
int dropbear_auth_initialize(void);
int signkey_generate(int,int,const char *,int);
void *m_malloc(size_t);
void *m_realloc(void *,size_t);
void m_free_direct(void *);
#define m_free(p) m_free_direct(p)
void m_malloc_set_epoch(unsigned);
void m_malloc_free_epoch(unsigned,int);
void m_burn(void *,size_t);
void setnonblocking(int);
char *buf_getstring(void *,unsigned *);
unsigned buf_getint(void *);
unsigned char buf_getbool(void *);
void buf_putbyte(void *,unsigned char);
void buf_putint(void *,unsigned);
void buf_putstring(void *,const char *,unsigned);
void encrypt_packet(void);
void send_msg_channel_success(void *);
void send_msg_channel_failure(void *);
void send_msg_userauth_failure(int,int);
void send_msg_userauth_success(void);
int dropbear_verify_password(const char *,const char *);
struct signame { const char *name; int signal; };
extern struct signame signames[];
typedef struct { unsigned char data[128]; } hash_state;
typedef struct { hash_state md; int hash; } hmac_state;
struct hash_description { unsigned hashsize,blocksize; };
extern struct hash_description hash_descriptor[];
int hash_is_valid(int);
void sha256_init(hash_state *);
void sha256_process(hash_state *,const void *,unsigned long);
void sha256_done(hash_state *,unsigned char *);
typedef int mp_int;
#define MP_LT -1
#define MP_GT 1
int mp_count_bits(const mp_int *);
int mp_cmp(const mp_int *,const mp_int *);
int mp_cmp_d(const mp_int *,int);
void bytes_to_mp(mp_int *,const unsigned char *,unsigned);
