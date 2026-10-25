#include "esp_idf_version.h"
#if ESP_IDF_VERSION_MAJOR != 6 || ESP_IDF_VERSION_MINOR != 1
#error "This witness requires the pinned IDF 6.1-derived headers"
#endif
/* Temporary emulator-only binary qualification; never part of Nxrs firmware. */
#include <nuttx/config.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include "esp_csi_gain_ctrl.h"
#include "esp_radar_motion_dec.h"

static unsigned checks, failures;
#define CHECK(name, expr) do { checks++; if (!(expr)) { failures++; printf("CSI_CHECK_FAIL %s line=%d\n",name,__LINE__); } } while(0)
static bool closef(float a,float b,float eps){return isfinite(a)&&isfinite(b)&&fabsf(a-b)<=eps;}
static void value(const char *n,float f){uint32_t u;memcpy(&u,&f,4);printf("CSI_VALUE %s %08lx\n",n,(unsigned long)u);}

/* Link wrapping intercepts allocation only during explicitly marked calls.
 * Successful allocations still use NuttX's allocator. All tracked blocks have
 * trailing guards checked on free. No concurrent tests enable this tracker. */
void *__real_malloc(size_t n); void __real_free(void *p);
static bool tracking;static unsigned alloc_calls,fail_at,live,peak,guard_errors;
static struct {void *p;size_t n;} slots[32];
void *__wrap_malloc(size_t n){
 if(!tracking)return __real_malloc(n);
 alloc_calls++;if(fail_at&&alloc_calls==fail_at)return NULL;
 void *p=__real_malloc(n+16);if(!p)return NULL;
 for(unsigned i=0;i<32;i++)if(!slots[i].p){slots[i].p=p;slots[i].n=n;memset((char*)p+n,0xa5,16);live++;if(live>peak)peak=live;return p;}
 guard_errors++;__real_free(p);return NULL;
}
void __wrap_free(void *p){
 if(p)for(unsigned i=0;i<32;i++)if(slots[i].p==p){for(unsigned j=0;j<16;j++)if(((uint8_t*)p)[slots[i].n+j]!=0xa5)guard_errors++;slots[i].p=NULL;live--;break;}
 __real_free(p);
}
static void track_begin(unsigned fail){alloc_calls=0;fail_at=fail;tracking=true;}
static unsigned track_end(void){tracking=false;fail_at=0;return alloc_calls;}

/* PHY recording stubs validate only the outbound scalar call ABI. */
static unsigned phy_calls,fft_calls;static int phy_enable,phy_gain,fft_enable,fft_gain;
void __wrap_phy_force_rx_gain(int enable,int gain){phy_calls++;phy_enable=enable;phy_gain=gain;}
void __wrap_phy_fft_scale_force(int enable,int gain){fft_calls++;fft_enable=enable;fft_gain=gain;}

static void gain_tests(void){
 uint8_t agc=0;int8_t fft=0;float factor=-9;
 esp_csi_gain_ctrl_reset_rx_gain_baseline();
 CHECK("gain initial collect",esp_csi_gain_ctrl_get_gain_status()==RX_GAIN_COLLECT);
 CHECK("gain before ready",esp_csi_gain_ctrl_get_gain_compensation(&factor,20,4)==ESP_ERR_INVALID_STATE);
 CHECK("baseline null arg",esp_csi_gain_ctrl_get_rx_gain_baseline(NULL,&fft)==ESP_ERR_INVALID_ARG);
 uint8_t packet[32];memset(packet,0x55,sizeof(packet));packet[22]=(uint8_t)-7;packet[23]=63;
 esp_csi_gain_ctrl_get_rx_gain(packet,&agc,&fft);CHECK("synthetic metadata signed bytes",agc==63&&fft==-7);
 agc=77;fft=33;esp_csi_gain_ctrl_get_rx_gain(NULL,&agc,&fft);
 CHECK("invalid gain extraction logging",agc==77&&fft==33);
 for(unsigned round=0;round<8;round++){
  esp_csi_gain_ctrl_reset_rx_gain_baseline();track_begin(0);
  for(unsigned i=0;i<49;i++)esp_csi_gain_ctrl_record_rx_gain(20,4);
  CHECK("gain collecting 49",esp_csi_gain_ctrl_get_gain_status()==RX_GAIN_COLLECT);
  esp_csi_gain_ctrl_record_rx_gain(20,4);unsigned calls=track_end();
  CHECK("gain baseline allocation freed",calls>0&&live==0&&guard_errors==0);
  CHECK("gain ready 50",esp_csi_gain_ctrl_get_gain_status()==RX_GAIN_READY);
  CHECK("baseline success",esp_csi_gain_ctrl_get_rx_gain_baseline(&agc,&fft)==ESP_OK&&agc==20&&fft==4);
  CHECK("gain unit factor",esp_csi_gain_ctrl_get_gain_compensation(&factor,20,4)==ESP_OK&&closef(factor,1,0.00001f));
 }
 CHECK("gain positive delta attenuation",esp_csi_gain_ctrl_get_gain_compensation(&factor,26,4)==ESP_OK&&closef(factor,0.5011872336272722f,0.00001f));
 value("gain_plus6",factor);
 CHECK("gain negative delta amplification",esp_csi_gain_ctrl_get_gain_compensation(&factor,14,4)==ESP_OK&&closef(factor,1.9952623149688795f,0.00001f));
 value("gain_minus6",factor);
 struct {uint32_t a;int8_t x[6];uint16_t b;} s8={0x1234abcd,{-128,-100,-1,0,100,127},0x5a5a};
 CHECK("8bit compensation",esp_csi_gain_ctrl_compensate_rx_gain(s8.x,sizeof(s8.x),false,&factor,14,4)==ESP_OK);
 CHECK("8bit saturation",s8.x[0]==-128&&s8.x[1]==-128&&s8.x[3]==0&&s8.x[4]==127&&s8.x[5]==127);
 CHECK("8bit guards",s8.a==0x1234abcd&&s8.b==0x5a5a);
 struct {uint32_t a;int16_t x[6];uint32_t b;} s16={0x1234abcd,{-32768,-30000,-1,0,30000,32767},0x5a5abcbc};
 CHECK("16bit compensation",esp_csi_gain_ctrl_compensate_rx_gain(s16.x,sizeof(s16.x),true,&factor,14,4)==ESP_OK);
 CHECK("16bit saturation",s16.x[0]==-32768&&s16.x[1]==-32768&&s16.x[3]==0&&s16.x[4]==32767&&s16.x[5]==32767);
 CHECK("16bit guards",s16.a==0x1234abcd&&s16.b==0x5a5abcbc);
 CHECK("force entrypoint",esp_csi_gain_ctrl_set_rx_force_gain(27,-3)==ESP_OK&&esp_csi_gain_ctrl_get_gain_status()==RX_GAIN_FORCE);
 CHECK("PHY recording ABI",phy_calls==1&&fft_calls==1&&phy_enable==1&&phy_gain==27&&fft_enable==1&&(fft_gain&255)==253);
 CHECK("release force",esp_csi_gain_ctrl_set_rx_force_gain(0,0)==ESP_OK&&phy_enable==0&&fft_enable==0&&phy_gain==0&&fft_gain==0&&esp_csi_gain_ctrl_get_gain_status()==RX_GAIN_READY);
 esp_csi_gain_ctrl_reset_rx_gain_baseline();CHECK("gain reset",esp_csi_gain_ctrl_get_gain_status()==RX_GAIN_COLLECT);
}

static float data[6][4]={{1,2,3,4},{2,4,6,8},{3,6,9,12},{4,8,12,16},{5,10,15,20},{6,12,18,24}};
static float reference_wander(const float *a,const float *b,unsigned n){
 double ma=0,mb=0,aa=0,bb=0,ab=0;for(unsigned i=0;i<n;i++){ma+=a[i];mb+=b[i];}ma/=n;mb/=n;
 for(unsigned i=0;i<n;i++){double x=a[i]-ma,y=b[i]-mb;aa+=x*x;bb+=y*y;ab+=x*y;}
 return (float)(1-fabs(ab/sqrt(aa*bb)));
}
static bool motion_once(void){float out[4];if(esp_radar_motion_dec_compute(4,3,data,3,data+3,out)!=ESP_OK)return false;
 for(unsigned c=0;c<4;c++)if(!closef(out[c],out[0]*(c+1),0.0005f))return false;
 return isfinite(out[0])&&out[0]>0;
}
static void motion_tests(void){
 struct {uint32_t a;float out[4];uint32_t b;} x={0x1234abcd,{0},0x5a5abcbc};float other[4];
 track_begin(0);int r=esp_radar_motion_dec_compute(4,3,data,3,data+3,x.out);unsigned count=track_end();
 printf("CSI_ALLOCATIONS motion=%u peak_blocks=%u\n",count,peak);
 CHECK("motion success",r==ESP_OK);CHECK("motion output guards",x.a==0x1234abcd&&x.b==0x5a5abcbc);
 CHECK("motion rankone numerical",motion_once());
 for(unsigned c=0;c<4;c++)value(c==0?"motion0":c==1?"motion1":c==2?"motion2":"motion3",x.out[c]);
 CHECK("motion split",esp_radar_motion_dec_compute(4,2,data,4,data+2,other)==ESP_OK);
 for(unsigned c=0;c<4;c++)CHECK("motion split invariant",closef(x.out[c],other[c],0.0005f));
 CHECK("motion allocations balanced",count>=4&&live==0&&guard_errors==0);
 for(unsigned f=1;f<=count;f++){
  track_begin(f);int err=esp_radar_motion_dec_compute(4,3,data,3,data+3,other);track_end();
  printf("CSI_FAULT allocation=%u result=%d live=%u guards=%u\n",f,err,live,guard_errors);
  CHECK("motion allocation failure reported",err==ESP_ERR_NO_MEM);
  CHECK("motion failure cleanup",live==0&&guard_errors==0);
  CHECK("motion recovery",motion_once());
 }
 for(unsigned i=0;i<200;i++){track_begin(0);bool ok=motion_once();track_end();CHECK("motion repeat",ok&&live==0&&guard_errors==0);}
 float a[]={1,2,3,4},b[]={4,1,3,2},neg[]={-2,-4,-6,-8};
 CHECK("wander identical",closef(esp_radar_motion_dec_wander_compute(a,a,4),0,0.00001f));
 CHECK("wander sign scale",closef(esp_radar_motion_dec_wander_compute(a,neg,4),0,0.00001f));
 float actual=esp_radar_motion_dec_wander_compute(a,b,4);value("wander",actual);
 CHECK("wander independent Pearson oracle",closef(actual,reference_wander(a,b,4),0.00001f));
 float *history[]={a,a,a,a};
 CHECK("jitter insufficient",esp_radar_motion_dec_compute_jitter(a,history,4,4,3)==0);
 CHECK("jitter identical",closef(esp_radar_motion_dec_compute_jitter(a,history,4,4,4),0,0.00001f));
 CHECK("jitter nonzero",closef(esp_radar_motion_dec_compute_jitter(b,history,4,4,5),actual,0.00001f));
}
struct worker{unsigned id;unsigned rounds;unsigned errors;};
static void *worker(void *v){struct worker *w=v;volatile float held=(w->id?17.25f:3.75f);float expected=held;
 for(unsigned i=0;i<100;i++){if(!motion_once())w->errors++;held=held*1.25f;sched_yield();held=held/1.25f;usleep(1000);if(!closef(held,expected,0.00001f))w->errors++;w->rounds++;}return NULL;}
static void realistic_window_tests(void){
 enum {ROWS=32,COLS=52};static float samples[ROWS][COLS];float out[COLS];
 for(unsigned r=0;r<ROWS;r++)for(unsigned c=0;c<COLS;c++)samples[r][c]=(float)(r+1)*(float)(c+1)/32.0f;
 for(unsigned round=0;round<10;round++){
  track_begin(0);int err=esp_radar_motion_dec_compute(COLS,16,samples,16,samples+16,out);track_end();
  CHECK("52carrier 32frame window",err==ESP_OK&&live==0&&guard_errors==0);
  for(unsigned c=0;c<COLS;c++)CHECK("52carrier numerical ratio",isfinite(out[c])&&closef(out[c],out[0]*(c+1),0.001f));
 }
 value("52carrier_first",out[0]);value("52carrier_last",out[51]);
}
static void thread_tests(void){pthread_t ids[2];struct worker w[2]={{0,0,0},{1,0,0}};int a=pthread_create(&ids[0],NULL,worker,&w[0]),b=pthread_create(&ids[1],NULL,worker,&w[1]);
 CHECK("pthread create",a==0&&b==0);if(a==0)CHECK("pthread join0",pthread_join(ids[0],NULL)==0);if(b==0)CHECK("pthread join1",pthread_join(ids[1],NULL)==0);
 printf("CSI_THREADS rounds=%u,%u errors=%u,%u\n",w[0].rounds,w[1].rounds,w[0].errors,w[1].errors);
 CHECK("concurrent motion and FP context",w[0].rounds==100&&w[1].rounds==100&&w[0].errors==0&&w[1].errors==0);
}
#include "lab-replay.c"
#include "numerical.c"
#include "nonfinite.c"
int main(int argc,char **argv){puts("CSI_PROBE_ENTERED pinned_idf=6.1 algorithms=6.0 synthetic_inputs=1 PHY=recording_stubs");
 if(argc>1&&strcmp(argv[1],"replay")==0)return lab_replay();
 if(argc>1&&strcmp(argv[1],"fail")==0){puts("CSI_DELIBERATE_FAILURE");return 7;}
 if(argc>1&&strcmp(argv[1],"numerics")==0){numerical_limit_tests();printf("CSI_NUMERICAL_REPORT checks=%u failures=%u guards=%u live=%u\n",checks,failures,guard_errors,live);return failures?1:0;}
 if(argc>1&&strcmp(argv[1],"nonfinite")==0){nonfinite_tests();printf("CSI_NONFINITE_REPORT checks=%u failures=%u guards=%u live=%u\n",checks,failures,guard_errors,live);return failures?1:0;}
 gain_tests();motion_tests();realistic_window_tests();thread_tests();
 printf("CSI_REPORT checks=%u failures=%u guards=%u live=%u\n",checks,failures,guard_errors,live);
 return failures?1:0;
}
