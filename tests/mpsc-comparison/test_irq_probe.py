from pathlib import Path
import subprocess
import tempfile
import unittest

import build
import irq_metrics
import placement

HERE = Path(__file__).resolve().parent


class IrqProbeTests(unittest.TestCase):
    def test_actual_rust_observer_lifecycle_and_emission(self):
        program = r'''
use std::sync::atomic::{AtomicU64,AtomicUsize,Ordering};
fn check(ok:bool,why:&str) {assert!(ok,"{why}");}
fn line(args:std::fmt::Arguments<'_>) {println!("{args}");}
#[path="PROTOCOL"] mod irq_protocol;
#[path="OBSERVER"] mod irq_probe;
static NEXT:AtomicU64=AtomicU64::new(100);
static PHASE:AtomicUsize=AtomicUsize::new(0);
static OPEN:AtomicUsize=AtomicUsize::new(0);
#[unsafe(no_mangle)] extern "C" fn mp_irq_probe_open(_:extern "C" fn(u64), info:*mut u32)->i32 {
 assert_eq!(OPEN.swap(1,Ordering::Relaxed),0);
 for (i,v) in [3,0,58,0x40380000,0x40380100,0x40380200,0x40380300].iter().enumerate() {
  unsafe {info.add(i).write(*v);}
 }
 0
}
#[unsafe(no_mangle)] extern "C" fn mp_irq_probe_now()->u64 {NEXT.load(Ordering::Relaxed)}
#[unsafe(no_mangle)] extern "C" fn mp_irq_probe_arm(next:u64)->i32 {NEXT.store(next,Ordering::Relaxed);0}
#[unsafe(no_mangle)] extern "C" fn mp_irq_probe_start(next:u64)->i32 {
 NEXT.store(next,Ordering::Relaxed);PHASE.fetch_add(1,Ordering::Relaxed);
 for _ in 0..300 {irq_probe::mpsc_irq_probe_entry(NEXT.load(Ordering::Relaxed)+10);}
 0
}
#[unsafe(no_mangle)] extern "C" fn mp_irq_probe_stop(end:*mut u64,pending:*mut u32)->i32 {
 unsafe {end.write(NEXT.load(Ordering::Relaxed)-1);pending.write(0);}0
}
#[unsafe(no_mangle)] extern "C" fn mp_irq_probe_close()->i32 {assert_eq!(OPEN.swap(0,Ordering::Relaxed),1);0}
fn main() {
 irq_probe::prepare();irq_probe::start_idle();irq_probe::finish_idle();
 irq_probe::start();irq_probe::finish();irq_probe::emit();
 assert_eq!(PHASE.load(Ordering::Relaxed),2);assert_eq!(OPEN.load(Ordering::Relaxed),0);
}
'''.replace('PROTOCOL', str(HERE / 'rust/src/irq_protocol.rs')).replace('OBSERVER', str(HERE / 'rust/src/irq_probe.rs'))
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / 'observer.rs', Path(directory) / 'observer'
            source.write_text(program)
            subprocess.run(['rustc', '--edition=2024', '-O', str(source), '-o', str(binary)],
                           check=True, capture_output=True)
            output = subprocess.check_output([str(binary)], text=True)
        import json
        rows = [json.loads(line[5:]) for line in output.splitlines()]
        self.assertEqual(len([r for r in rows if r['record']=='irq-sample']), 512)
        phases = [r for r in rows if r['record']=='irq-phase']
        self.assertEqual({p['phase'] for p in phases}, {'idle','loaded'})
        self.assertTrue(all(p['observed']==300 and p['dropped']==44 and p['error']==0 for p in phases))
        self.assertTrue(rows[0]['cleaned_up'])

    def test_actual_rust_protocol_wrap_skip_loss_and_stop_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'protocol'
            subprocess.run(['rustc', '--edition=2024', '--test', str(HERE / 'rust/src/irq_protocol.rs'),
                            '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_actual_adapter_ownership_ack_quiescence_and_release(self):
        # Execute the production adapter body against a register/allocator
        # model. Actual SDK types/register names are separately target-compiled;
        # these enum addresses deliberately model behavior, not a second ABI.
        prelude = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <assert.h>
#define MPSC_IRQ_HOST_TEST 1
#define MP_IRQ_IRAM
#define MP_IRQ_INTERNAL(f) (!bad_address)
#define MP_IRQ_OS_CLAIMED() os_claimed
enum {SYSTIMER_CONF_REG, SYSTIMER_INT_ENA_REG, SYSTIMER_INT_RAW_REG,
 SYSTIMER_INT_CLR_REG, SYSTIMER_UNIT0_OP_REG, SYSTIMER_UNIT0_VALUE_HI_REG,
 SYSTIMER_UNIT0_VALUE_LO_REG, SYSTIMER_TARGET1_HI_REG, SYSTIMER_TARGET1_LO_REG,
 SYSTIMER_COMP1_LOAD_REG, SYSTIMER_TARGET1_CONF_REG,
 INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG, REG_COUNT};
#define SYSTIMER_TARGET1_WORK_EN (1u<<23)
#define SYSTIMER_CLK_EN (1u<<31)
#define SYSTIMER_TIMER_UNIT0_WORK_EN (1u<<30)
#define SYSTIMER_TIMER_UNIT0_UPDATE (1u<<30)
#define SYSTIMER_TIMER_UNIT0_VALUE_VALID (1u<<29)
#define SYSTIMER_TIMER_UNIT0_VALUE_HI 0xfffffu
#define SYSTIMER_TIMER_COMP1_LOAD 1u
#define SYSTIMER_TARGET1_INT_ENA 2u
#define SYSTIMER_TARGET1_INT_RAW 2u
#define SYSTIMER_TARGET1_INT_CLR 2u
#define SYSTIMER_TARGET1_PERIOD_MODE (1u<<30)
#define SYSTIMER_TARGET1_TIMER_UNIT_SEL (1u<<31)
#define XCHAL_INTLEVEL1_MASK 0x637ffu
#define ETS_SYSTIMER_TARGET1_INTR_SOURCE 58
#define ESP_INTR_FLAG_LEVEL1 (1u<<1)
#define ESP_INTR_FLAG_IRAM (1u<<10)
#define ESP_INTR_FLAG_INTRDISABLED (1u<<11)
typedef void *intr_handle_t;
static uint32_t regs[REG_COUNT];
static bool os_claimed, claimed, bad_address, valid_counter=true, replacement;
static int allocation_error, free_error, enable_error, cpu, cpuint=3, released, writes_after_release;
static unsigned depth, allocations, frees, callbacks;
static uint64_t now, last_entry;
static unsigned lock(void) {unsigned previous=depth;depth++;return previous;}
static void unlock(unsigned previous) {assert(depth>previous);depth=previous;}
#define MP_IRQ_LOCK() lock()
#define MP_IRQ_UNLOCK(flags) unlock(flags)
#define MP_IRQ_READ(address) regs[address]
static void write_reg(unsigned address,uint32_t value) {
 if(released)writes_after_release++;
 if(address==SYSTIMER_INT_CLR_REG) {assert(value==2u);regs[SYSTIMER_INT_RAW_REG]&=~value;}
 regs[address]=value;
 if(address==SYSTIMER_UNIT0_OP_REG) {
   regs[address]=valid_counter?SYSTIMER_TIMER_UNIT0_VALUE_VALID:0;
   regs[SYSTIMER_UNIT0_VALUE_HI_REG]=(uint32_t)(now>>32);
   regs[SYSTIMER_UNIT0_VALUE_LO_REG]=(uint32_t)now;
 }
}
#define MP_IRQ_WRITE(address,value) write_reg(address,value)
static int esp_intr_alloc(int source,int flags,void (*isr)(void*),void *arg,intr_handle_t *handle) {
 (void)isr;(void)arg;allocations++;
 assert(source==58 && flags==(ESP_INTR_FLAG_LEVEL1|ESP_INTR_FLAG_IRAM|ESP_INTR_FLAG_INTRDISABLED));
 if(allocation_error)return allocation_error;
 if(claimed)return -12;
 claimed=true;*handle=(void*)1;regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=6;return 0;
}
static int esp_intr_get_cpu(intr_handle_t h) {(void)h;return cpu;}
static int esp_intr_get_intno(intr_handle_t h) {(void)h;return cpuint;}
static int esp_intr_enable(intr_handle_t h) {(void)h;if(enable_error)return enable_error;regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=cpuint;return 0;}
static int esp_intr_disable(intr_handle_t h) {(void)h;regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=6;return 0;}
static int esp_intr_free(intr_handle_t h) {
 assert(h);
 frees++;if(free_error)return free_error;
 claimed=false;released=1;regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=6;
 if(replacement) {regs[SYSTIMER_TARGET1_CONF_REG]=0x5678;regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=9;}
 return 0;
}
'''
        tests = r'''
static void callback(uint64_t entered) {callbacks++;last_entry=entered;assert(mp_irq_probe_arm(entered+1600)==0);}
static void reset(void) {
 memset(regs,0,sizeof(regs));mp_irq_handle=NULL;mp_irq_callback=NULL;mp_irq_active=false;
 os_claimed=claimed=bad_address=replacement=false;valid_counter=true;
 allocation_error=free_error=enable_error=cpu=released=writes_after_release=0;
 cpuint=3;depth=allocations=frees=callbacks=0;now=100;
 regs[SYSTIMER_CONF_REG]=SYSTIMER_CLK_EN|SYSTIMER_TIMER_UNIT0_WORK_EN|(1u<<24)|(1u<<22);
 regs[SYSTIMER_INT_ENA_REG]=5;regs[SYSTIMER_INT_RAW_REG]=5;
 regs[SYSTIMER_TARGET1_CONF_REG]=0xc0000123;regs[SYSTIMER_TARGET1_HI_REG]=7;regs[SYSTIMER_TARGET1_LO_REG]=42;
 regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=16;
}
int main(void) {
 uint32_t info[7], pending;uint64_t end;
 for(unsigned condition=0;condition<8;condition++) {
  reset();
  switch(condition) {
   case 0:regs[SYSTIMER_CONF_REG]|=SYSTIMER_TARGET1_WORK_EN;break;
   case 1:regs[SYSTIMER_INT_ENA_REG]|=2;break;
   case 2:regs[SYSTIMER_INT_RAW_REG]|=2;break;
   case 3:regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]=7;break;
   case 4:os_claimed=true;break;
   case 5:regs[SYSTIMER_CONF_REG]&=~SYSTIMER_TIMER_UNIT0_WORK_EN;break;
   case 6:bad_address=true;break;
   case 7:claimed=true;break;
  }
  uint32_t saved[REG_COUNT];memcpy(saved,regs,sizeof(regs));
  assert(mp_irq_probe_open(callback,info)!=0 && memcmp(saved,regs,sizeof(regs))==0);
  assert(!mp_irq_handle && !depth);
 }
 reset();allocation_error=-22;assert(mp_irq_probe_open(callback,info)==-22 && !mp_irq_handle);
 reset();cpuint=24;assert(mp_irq_probe_open(callback,info)!=0 && frees==1 && !writes_after_release);
 reset();assert(mp_irq_probe_open(callback,info)==0 && info[0]==3 && info[1]==0 && info[2]==58);
 assert(mp_irq_probe_open(callback,info)!=0 && allocations==1);
 uint32_t original=regs[SYSTIMER_CONF_REG];
 assert(mp_irq_probe_start(1700)==0 && mp_irq_active);
 assert((regs[SYSTIMER_TARGET1_CONF_REG]&0xc0000000u)==0);
 assert((regs[SYSTIMER_CONF_REG]&~SYSTIMER_TARGET1_WORK_EN)==original);
 assert(regs[SYSTIMER_INT_ENA_REG]==7 && regs[SYSTIMER_INT_RAW_REG]==5);
 now=1711;regs[SYSTIMER_INT_RAW_REG]|=2;mp_irq_probe_isr(NULL);
 assert(callbacks==1 && last_entry==1711 && regs[SYSTIMER_INT_RAW_REG]==5);
 assert(regs[SYSTIMER_TARGET1_LO_REG]==3311 && !depth);
 now=3400;regs[SYSTIMER_INT_RAW_REG]|=2;
 assert(mp_irq_probe_stop(&end,&pending)==0 && end==3400 && pending==1 && !mp_irq_active);
 assert(regs[SYSTIMER_CONF_REG]==original && regs[SYSTIMER_INT_ENA_REG]==5 && regs[SYSTIMER_INT_RAW_REG]==5);
 assert(mp_irq_probe_arm(5000)!=0 && mp_irq_probe_close()==0);
 assert(regs[SYSTIMER_TARGET1_CONF_REG]==0xc0000123 && regs[SYSTIMER_TARGET1_HI_REG]==7 && regs[SYSTIMER_TARGET1_LO_REG]==42);
 assert(!mp_irq_handle && !claimed && !writes_after_release && !depth);
 reset();assert(mp_irq_probe_open(callback,info)==0);replacement=true;
 assert(mp_irq_probe_close()==0 && regs[SYSTIMER_TARGET1_CONF_REG]==0x5678 && regs[INTERRUPT_CORE0_SYSTIMER_TARGET1_INT_MAP_REG]==9 && !writes_after_release);
 reset();assert(mp_irq_probe_open(callback,info)==0);enable_error=-7;
 assert(mp_irq_probe_start(1700)==-7 && !mp_irq_active && !mp_irq_target_busy());
 assert(mp_irq_probe_close()==0);
 reset();assert(mp_irq_probe_open(callback,info)==0);free_error=-8;
 assert(mp_irq_probe_close()==-8 && mp_irq_handle && claimed);
 free_error=0;assert(mp_irq_probe_close()==0);
 reset();valid_counter=false;assert(mp_irq_probe_now()==UINT64_MAX);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / 'adapter.c', Path(directory) / 'adapter'
            source.write_text(prelude + '\n#include "' + str(HERE / 'boards/esp32s3/irq_probe.c') + '"\n' + tests)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    @staticmethod
    def observation():
        info = dict(record='irq-observer', scope='hardware-alarm-to-probe-entry', counter_hz=16_000_000,
                    target=1, unit=0, priority=1, cpu_interrupt=3, core=0, source=58,
                    isr_address=0x40380000, callback_address=0x40380100, read_address=0x40380200,
                    arm_address=0x40380300, roots_internal=True, cleaned_up=True,
                    external_clock_calibrated=False, sample_capacity=256,
                    sample_storage_bytes=12288, rust_state_bytes=12448,
                    sample_policy='first-samples-plus-all-event-max')
        rows = [info]
        for name, count in (('idle', 1000), ('loaded', 10)):
            rows.append(dict(record='irq-phase', phase=name, start=0, end=count*1600+20,
                             period_ticks=1600, observed=count, missed_periods=0, trailing_periods=0,
                             stored=min(count,256), dropped=max(0,count-256), maximum_lateness_ticks=10,
                             pending_at_stop=False, error=0))
            rows += [dict(record='irq-sample', phase=name, index=i, deadline=(i+1)*1600,
                          entered=(i+1)*1600+10, lateness_ticks=10, skipped_periods=0)
                     for i in range(min(count,256))]
        metadata = dict(observer='irq', native=True, clock_hz=240000000,
                        idle_ticks=24_000_000, wall_ns=1_000_000)
        return rows, metadata

    def test_replay_keeps_censored_samples_and_unserviced_deadlines_visible(self):
        rows, metadata = self.observation()
        result = irq_metrics.analyze(rows, metadata)
        self.assertEqual(result['phases']['idle']['dropped'], 744)
        self.assertEqual(result['phases']['idle']['retained_first_sample_lateness_ticks']['count'], 256)
        self.assertFalse(result['phases']['idle']['full_sample_distribution_available'])
        self.assertFalse(result['probe_path_placement_qualified'])
        rows = [r for r in rows if r['record'] != 'irq-sample' or r['phase'] != 'loaded']
        p = next(r for r in rows if r['record'] == 'irq-phase' and r['phase'] == 'loaded')
        p.update(observed=0, stored=0, trailing_periods=10, maximum_lateness_ticks=0, pending_at_stop=True)
        loaded = irq_metrics.analyze(rows, metadata)['phases']['loaded']
        self.assertIsNone(loaded['maximum_serviced_lateness_ticks'])
        self.assertIsNone(loaded['retained_first_sample_lateness_ticks'])
        self.assertEqual(loaded['trailing_periods'], 10)

    def test_replay_rejects_bad_scope_cadence_counts_and_false_cleanup(self):
        for kind, field, value in (
            ('irq-observer', 'target', 2), ('irq-observer', 'cleaned_up', False),
            ('irq-observer', 'cpu_interrupt', 24), ('irq-observer', 'callback_address', 0x42000000),
            ('irq-phase', 'missed_periods', 1), ('irq-phase', 'dropped', 0),
            ('irq-phase', 'maximum_lateness_ticks', 0), ('irq-phase', 'error', 1),
            ('irq-sample', 'deadline', 1), ('irq-sample', 'skipped_periods', 1)):
            rows, metadata = self.observation()
            next(r for r in rows if r['record'] == kind)[field] = value
            with self.subTest(kind=kind, field=field), self.assertRaises(ValueError):
                irq_metrics.analyze(rows, metadata)
        rows, metadata = self.observation()
        with self.assertRaisesRegex(ValueError, 'linked root'):
            irq_metrics.analyze(rows, metadata, {field: 0x40399900 for field in irq_metrics.ADDRESS_FIELDS.values()})
        metadata['observer'] = 'primary'
        with self.assertRaisesRegex(ValueError, 'unexpected IRQ'):
            irq_metrics.analyze(rows, metadata)

    def test_irq_placement_includes_cold_helpers_and_preserves_indirect_boundaries(self):
        disassembly = '''40380000 <mp_irq_probe_isr>:
40380000: 000005 call8 40380100 <mp_irq_probe_now>
40380003: 0000e0 callx8 a8
40380100 <mp_irq_probe_now>:
40380100: f00d retw.n
40380200 <mp_irq_probe_arm>:
40380200: f00d retw.n
40380300 <mpsc_irq_probe_entry>:
40380300: 000005 call8 42000000 <core::panicking::panic_bounds_check>
42000000 <core::panicking::panic_bounds_check>:
42000000: f00d retw.n
'''
        audit = placement.inspect_disassembly(disassembly, 'esp32s3', 'native', irq_probe=True)
        self.assertEqual(audit['missing_roots'], [])
        self.assertFalse(audit['direct_paths_in_sram'])
        self.assertEqual(audit['excluded_cold'], [])
        self.assertEqual(audit['flash_boundaries'][0]['name'], 'core::panicking::panic_bounds_check')
        self.assertEqual(len(audit['indirect_calls']), 1)
        self.assertEqual(build.irq_root_addresses(audit)['isr_address'], 0x40380000)
        audit['roots'][0]['memory'] = 'flash'
        with self.assertRaises(ValueError): build.irq_root_addresses(audit)


if __name__ == '__main__':
    unittest.main()
