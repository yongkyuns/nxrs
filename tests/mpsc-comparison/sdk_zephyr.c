#include <zephyr/kernel.h>
#include <zephyr/kernel_structs.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/timing/timing.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static struct k_sem gates[20];
static struct k_msgq queue;
static uint8_t queue_storage[64*64] __aligned(4);
static struct k_thread workers[5];
K_THREAD_STACK_ARRAY_DEFINE(stacks,5,4096);
struct entry_args {void (*entry)(uintptr_t,uintptr_t);uintptr_t context,role;};
static struct entry_args args[5];
static void (*deadline_callback)(void);
static void (*pulse_callback)(void);
static void pulse_expired(struct k_timer *ignored) {(void)ignored;pulse_callback();}
K_TIMER_DEFINE(pulse,pulse_expired,NULL);
uint64_t mp_native_ticks(void) {return k_uptime_ticks();}
int mp_pulse_init(void (*callback)(void)) {pulse_callback=callback;return 0;}
int mp_pulse_arm(uint32_t ticks) {k_timer_start(&pulse,K_TIMEOUT_ABS_TICKS(ticks),K_NO_WAIT);return 0;}
int mp_pulse_stop(void) {k_timer_stop(&pulse);return 0;}
extern void mpsc_entry(void);
static void expired(struct k_timer *ignored) {(void)ignored;deadline_callback();}
K_TIMER_DEFINE(watchdog,expired,NULL);
static void entry(void *p,void *unused1,void *unused2) {(void)unused1;(void)unused2;struct entry_args *a=p;a->entry(a->context,a->role);}
int mp_spawn(void (*function)(uintptr_t,uintptr_t),uintptr_t context,uintptr_t role) {unsigned i=role-1;args[i]=(struct entry_args){function,context,role};return k_thread_create(&workers[i],stacks[i],K_THREAD_STACK_SIZEOF(stacks[i]),entry,&args[i],NULL,NULL,20,0,K_NO_WAIT)?0:-1;}
int mp_join(uintptr_t role) {return k_thread_join(&workers[role-1],K_SECONDS(60));}
uint64_t mp_monotonic_ns(void) {return mp_wall_ns();}
int mp_sleep_ns(uint64_t ns) {k_sleep(K_TIMEOUT_ABS_TICKS(k_uptime_ticks()+k_ns_to_ticks_ceil64(ns)));return 0;}
int mp_get_schedule(int *v) {v[0]=0;v[1]=k_thread_priority_get(k_current_get());return 0;}
int mp_set_schedule(int policy,int priority) {(void)policy;k_thread_priority_set(k_current_get(),priority);return 0;}
uintptr_t mp_tid(void) {return (uintptr_t)k_current_get();}
int mp_waiting(uintptr_t tid) {struct k_thread *t=(struct k_thread *)tid;return k_thread_priority_get(t)==20&&(t->base.thread_state&_THREAD_PENDING)&&t->base.pended_on==&queue.wait_q;}
/* Zephyr uses one wait queue for blocked producers and the unique receiver.
 * The Rust phase proves the direction; this witness proves object identity. */
int mp_queue_waiting(uintptr_t tid,int sending) {(void)sending;return mp_waiting(tid);}
int mp_gate_init(size_t i) {return k_sem_init(&gates[i],0,UINT32_MAX);}
int mp_gate_post(size_t i) {k_sem_give(&gates[i]);return 0;}
int mp_gate_wait(size_t i) {return k_sem_take(&gates[i],K_SECONDS(60));}
int mp_gate_destroy(size_t i) {return k_sem_count_get(&gates[i])==0?0:-1;}
int mp_queue_create(size_t capacity,size_t bytes) {return k_msgq_init(&queue,(char *)queue_storage,bytes,capacity),0;}
int mp_queue_send(const uint8_t *data,int blocking) {int r=k_msgq_put(&queue,data,blocking?K_FOREVER:K_NO_WAIT);return r==-ENOMSG?1:r;}
int mp_queue_receive(uint8_t *data) {return k_msgq_get(&queue,data,K_FOREVER);}
int mp_queue_destroy(void) {return k_msgq_cleanup(&queue);}
uint64_t mp_runtime(uintptr_t tid) {
#ifdef CONFIG_THREAD_RUNTIME_STATS
  k_thread_runtime_stats_t s;if(k_thread_runtime_stats_get((k_tid_t)tid,&s))return UINT64_MAX;
  return timing_cycles_to_ns(s.execution_cycles);
#else
  (void)tid;return UINT64_MAX;
#endif
}
int mp_stack(uintptr_t tid,uint32_t *v) {struct k_thread *t=(struct k_thread *)tid;size_t unused;if(k_thread_stack_space_get(t,&unused))return -1;v[0]=t->stack_info.size;v[1]=v[0]-unused;v[2]=t->stack_info.start>=MP_RAM_BEGIN&&t->stack_info.start+v[0]<=MP_RAM_END;return 0;}
void mp_heap(uint32_t *v) {memset(v,0,4*sizeof(*v));}
int mp_watchdog_start(uint32_t ms,void (*callback)(void)) {deadline_callback=callback;k_timer_start(&watchdog,K_MSEC(ms),K_NO_WAIT);return 0;}
int mp_watchdog_stop(void) {k_timer_stop(&watchdog);return 0;}
int mp_output(const uint8_t *p,size_t bytes) {return fwrite(p,1,bytes,stdout)==bytes?0:-1;}
int mp_read(void) {unsigned char c;return uart_poll_in(DEVICE_DT_GET(DT_CHOSEN(zephyr_console)),&c)?-1:c;}
void mp_fatal(void) {k_panic();}
int main(void) {mp_console_init();mpsc_entry();mp_console_idle();return 0;}
