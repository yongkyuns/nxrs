/* Target-header SDK translations only. Rust owns the experiment and lifetime. */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/mqueue.h>
#include <nuttx/sched.h>
#include <nuttx/semaphore.h>
#include <nuttx/wdog.h>
#include <sched.h>
#include <semaphore.h>
#include <mqueue.h>
#include <malloc.h>
#include <time.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <stdlib.h>
#include <assert.h>

static sem_t gates[20];
static int sender=-1,receiver=-1,nonblocking=-1;
static size_t message_bytes;
static struct wdog_s watchdog;
static struct wdog_s pulse;
static void (*pulse_callback)(void);
static void pulse_expired(wdparm_t ignored) {(void)ignored;pulse_callback();}
uint64_t mp_native_ticks(void) {return clock_systime_ticks();}
int mp_pulse_init(void (*callback)(void)) {pulse_callback=callback;return 0;}
int mp_pulse_arm(uint32_t ticks) {return wd_start_abstick(&pulse,(clock_t)ticks,pulse_expired,0);}
int mp_pulse_stop(void) {return wd_cancel(&pulse);}
static void (*deadline_callback)(void);
static void expired(wdparm_t ignored) { (void)ignored; deadline_callback(); }
uint64_t mp_monotonic_ns(void) {return mp_wall_ns();}
int mp_sleep_ns(uint64_t ns) {struct timespec t={ns/1000000000u,ns%1000000000u};return nanosleep(&t,NULL);}
int mp_get_schedule(int *v) {struct sched_param p;if(sched_getparam(0,&p)<0)return errno;v[0]=sched_getscheduler(0);v[1]=p.sched_priority;return v[0]<0?errno:0;}
int mp_set_schedule(int policy,int priority) {struct sched_param p={.sched_priority=priority};return sched_setscheduler(0,policy,&p)<0?errno:0;}
uintptr_t mp_tid(void) {return gettid();}
int mp_waiting(uintptr_t tid) {struct tcb_s *t=nxsched_get_tcb(tid);return t&&t->sched_priority==180&&(t->task_state==TSTATE_WAIT_SEM||t->task_state==TSTATE_WAIT_MQNOTFULL||t->task_state==TSTATE_WAIT_MQNOTEMPTY);}
int mp_gate_init(size_t i) {int r=nxsem_init(&gates[i],0,0);return r<0?r:nxsem_set_protocol(&gates[i],SEM_PRIO_NONE);}
int mp_gate_post(size_t i) {return nxsem_post(&gates[i]);}
/* Match Zephyr K_SECONDS(60): relative native ticks, retaining the same bound. */
int mp_gate_wait(size_t i) {return nxsem_tickwait(&gates[i],SEC2TICK(60));}
int mp_gate_destroy(size_t i) {return nxsem_destroy(&gates[i]);}
int mp_queue_create(size_t capacity,size_t bytes) {
  struct mq_attr a={.mq_maxmsg=capacity,.mq_msgsize=bytes};message_bytes=bytes;
  sender=nxmq_open("mpsc_perf",O_CREAT|O_EXCL|O_WRONLY,0600,&a);
  if(sender<0)return sender;
  receiver=nxmq_open("mpsc_perf",O_RDONLY);
  if(receiver<0)return receiver;
  nonblocking=nxmq_open("mpsc_perf",O_WRONLY|O_NONBLOCK);
  return nonblocking<0?nonblocking:0;
}
int mp_queue_send(const uint8_t *data,int blocking) {
  /* Each call uses an immutable descriptor: no shared flag mutation. */
  int r=nxmq_send(blocking?sender:nonblocking,(const char *)data,message_bytes,0);
  return r==-EAGAIN?1:r;
}
int mp_queue_receive(uint8_t *data) {ssize_t n=nxmq_receive(receiver,(char *)data,message_bytes,NULL);return n==(ssize_t)message_bytes?0:n<0?(int)n:-EIO;}
int mp_queue_destroy(void) {int a=nxmq_close(receiver),b=nxmq_close(sender),c=nxmq_close(nonblocking),d=nxmq_unlink("mpsc_perf");return a?a:b?b:c?c:d;}
uint64_t mp_runtime(uintptr_t tid) {
#if defined(CONFIG_SCHED_CRITMONITOR) && CONFIG_SCHED_CRITMONITOR_MAXTIME_THREAD >= 0
  irqstate_t flags=enter_critical_section();struct tcb_s *t=nxsched_get_tcb(tid);
  uint64_t result=UINT64_MAX;
  if(t) {clock_t value=t->run_time;if(tid==(uintptr_t)gettid())value+=perf_gettime()-t->run_start;result=(uint64_t)value*1000000000u/perf_getfreq();}
  leave_critical_section(flags);return result;
#else
  (void)tid;return UINT64_MAX;
#endif
}
int mp_stack(uintptr_t tid,uint32_t *v) {struct tcb_s *t=nxsched_get_tcb(tid);if(!t)return ESRCH;uintptr_t base=(uintptr_t)t->stack_base_ptr;v[0]=t->adj_stack_size;v[1]=up_check_tcbstack(t,t->adj_stack_size);v[2]=base>=MP_RAM_BEGIN&&base+v[0]<=MP_RAM_END;return 0;}
void mp_heap(uint32_t *v) {struct mallinfo m=mallinfo();v[0]=m.uordblks;v[1]=m.usmblks;v[2]=m.fordblks;v[3]=m.mxordblk;}
int mp_watchdog_start(uint32_t ms,void (*callback)(void)) {deadline_callback=callback;return wd_start(&watchdog,MSEC2TICK(ms),expired,0);}
int mp_watchdog_stop(void) {return wd_cancel(&watchdog);}
int mp_output(const uint8_t *p,size_t bytes) {return fwrite(p,1,bytes,stdout)==bytes?0:-1;}
int mp_read(void) {return -1;}
void mp_fatal(void) {PANIC();for(;;){}}

int mp_console_nonblocking(void) {return O_NONBLOCK;}
const char *mp_console_path(void) {return MP_CONSOLE_PATH;}
