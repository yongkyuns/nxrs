#include "includes.h"
#include <fcntl.h>
#include <sys/ioctl.h>
#include <crypto/cryptodev.h>
static int mode, opens, closes, sessions, releases, operations;
struct hash_description hash_descriptor[] = {{32,64}};
int hash_is_valid(int hash) { return hash == 0 ? CRYPT_OK : CRYPT_INVALID_HASH; }
static int device_open(const char *path,int flags,...)
{ assert(strcmp(path,"/dev/crypto")==0 && flags==O_RDWR); opens++; return 40; }
static int device_close(int fd) { assert(fd==40 || fd==41); closes++; return 0; }
static int device_fcntl(int fd,int cmd,...)
{ assert(fd==41 && cmd==F_SETFD); return mode==1 ? -1:0; }
static int device_ioctl(int fd,unsigned long request,...)
{
  va_list ap;va_start(ap,request);void *arg=va_arg(ap,void *);va_end(ap);
  if(request==CRIOGET) { assert(fd==40); *(int *)arg=41; return 0; }
  assert(fd==41);
  if(request==CIOCGSESSION) { sessions++; if(mode==2)return -1; ((struct session_op *)arg)->ses=77; return 0; }
  if(request==CIOCFSESSION) { assert(*(uint32_t *)arg==77);releases++;return 0; }
  assert(request==CIOCCRYPT);operations++;
  struct crypt_op *op=arg;assert(op->ses==77);
  if(mode==3 || (mode==4 && !op->flags))return -1;
  if(!op->flags)memset(op->mac,0x5a,32);
  return 0;
}
#define open device_open
#define close device_close
#define fcntl device_fcntl
#define ioctl device_ioctl
#include "netutils/dropbear/port/dropbear_ltc_hmac_sha256.c"
#undef open
#undef close
#undef fcntl
#undef ioctl
int main(void)
{
  for(mode=0;mode<=4;mode++)
    {
      hmac_state h;unsigned char out[32];unsigned long len=sizeof(out);
      opens=closes=sessions=releases=operations=0;
      int result=hmac_init(&h,0,(unsigned char *)"key",3);
      if(mode==1 || mode==2)assert(result==CRYPT_ERROR);
      else
        {
          assert(result==CRYPT_OK);
          result=hmac_process(&h,(unsigned char *)"data",4);
          if(mode==3)assert(result==CRYPT_ERROR && releases==1 && closes==2);
          else assert(result==CRYPT_OK);
        }
      result=hmac_done(&h,out,&len);
      if(mode==0)assert(result==CRYPT_OK && len==32 && out[0]==0x5a);
      else assert(result==CRYPT_ERROR);
      assert(closes==2 && releases==(mode<2?mode==0:mode>2));
      unsigned before=(unsigned)closes;
      assert(hmac_done(&h,out,&len)==CRYPT_ERROR && (unsigned)closes==before);
    }
  return 0;
}
