"""Refusal controls for evidence that could otherwise yield plausible timings."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('p4_capture_tests',Path(__file__).parent/'capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
analyze = capture.analysis


def fixture():
    meta = dict(record='metadata',schema='event-path-trace-v1',build_id='a'*64,
                transport='crossbeam-direct',samples=1,warmup=0,instrumented=True,recording=True,
                sender_tid=20,receiver_tid=21,sender_priority=120,receiver_priority=180,
                policy=1,clock_hz=240_000_000,counter_bits=32,counter_source=5,
                window_ns=1_000_000,trace_capacity=4096,trace_bytes=65544,trace_address=1024,trace_region=1)
    rows = [meta]
    rows += [dict(record='placement',role=role,address=2048,bytes=16384,region=2)
             for role in ('sender-stack','receiver-stack')]
    rows += [dict(record='allocation',scope='channel-constructor',address=4096,bytes=1024,alignment=8,
                  released_during_constructor=False,region=1)]
    rows += [dict(record='sample',sequence=1,start=100,admission_return=220,receive_return=200,
                  window_ns=10000,blocked_verified=True)]
    events = [(16,20,1,10),(12,21,1,20),(2,21,1,30),(14,20,1,80),
              (10,20,1,110),(1,21,1,120),(3,21,0,150),(13,21,1,210),
              (11,20,1,230),(15,20,1,240)]
    rows += [dict(record='event',index=i,kind=k,tid=t,detail=d,ticks=c)
             for i,(k,t,d,c) in enumerate(events)]
    rows += [dict(record='result',success=True,sample_count=1,attempted=1,handled=1,
                  checksum=analyze.checksum(1),event_count=len(events),overflow=False,
                  terminal_quiescence_verified=True,threads_joined=True,watchdog_joined=True,
                  schedule_restored=True,physical_timing_qualified=False,priority_progress_qualified=False)]
    return rows


def run(rows):
    return analyze.analyze('\n'.join(analyze.PREFIX+json.dumps(r) for r in rows),
                           {'build_id':'a'*64,'mode':'trace'},backend='crossbeam',policy='unequal',
                           recording=True,samples=1,warmup=0)


class TraceControls(unittest.TestCase):
    def test_prompt_accepts_only_the_nsh_erase_line_suffix(self):
        for data in (b'output\r\nnsh> ',b'output\r\nnsh> \x1b[K'):
            self.assertTrue(capture.final_prompt(data))
        for data in (b'not-nsh> ',b'\nnsh> \x1b[Klate fault',b'\nnsh> \x1b[2J'):
            self.assertFalse(capture.final_prompt(data))

    def test_comparison_rejects_mixed_workloads_and_unrelated_config(self):
        comparison=capture.load('p4_compare_tests',Path(__file__).parent/'compare.py')
        for invalid in (None,'workload','config','extra-note-driver'):
            summaries=[run(fixture()) for _ in range(3)]
            summaries[0].update(mode='baseline',recording=False)
            summaries[1]['recording']=False
            records=[{key:'same' for key in ('source_sha256','kernel_revision','apps_revision','compiler_identity',
                         'std_array_sha256','std_library_tree_sha256','crossbeam','build_environment')} for _ in range(3)]
            for record in records: record.update(build_id='a'*64,config_file='resolved.config')
            configs=[{'CONFIG_FREQ':'240','CONFIG_SCHED_INSTRUMENTATION':mode} for mode in ('n','y','y')]
            for config in configs[1:]: config['CONFIG_NOTE_RTT']='n'; config['CONFIG_SEGGER_SYSVIEW']='n'
            if invalid=='workload': summaries[1]['policy']='equal'
            if invalid=='config': configs[1]['CONFIG_FREQ']='160'
            if invalid=='extra-note-driver': configs[1]['CONFIG_NOTE_RTT']='y'
            with patch.object(comparison.capture,'receipt',side_effect=records), \
                 patch.object(comparison.capture,'replay',side_effect=summaries), \
                 patch.object(comparison.capture.p2,'checked_file',return_value=Path('ignored')), \
                 patch.object(comparison.capture.builder.p2.baseline,'config_values',side_effect=configs):
                if invalid:
                    with self.assertRaises(ValueError): comparison.compare([None]*3,[None]*3)
                else:
                    result=comparison.compare([None]*3,[None]*3)
                    self.assertTrue(result['matched']);self.assertFalse(result['pooled'])

    def test_both_quiet_arms_and_real_transport_names(self):
        for mode in ('baseline','trace'):
            for backend,name in [('std','raw-std'),('messaging','messaging'),('crossbeam','crossbeam-direct')]:
                rows=[r for r in fixture() if r['record']!='event']
                rows[0].update(instrumented=mode=='trace',recording=False,transport=name)
                rows[-1]['event_count']=0
                result=analyze.analyze('\n'.join(analyze.PREFIX+json.dumps(r) for r in rows),
                    {'build_id':'a'*64,'mode':mode},backend=backend,policy='unequal',recording=False,samples=1,warmup=0)
                self.assertEqual(result['transport'],backend)
                self.assertEqual(result['correlated_ticks'],{})

    def test_baseline_disables_parent_without_requesting_hidden_children(self):
        self.assertEqual(capture.builder.instrumentation_options('baseline'),
                         ['--kconfig-disable','SCHED_INSTRUMENTATION'])
        trace=capture.builder.instrumentation_options('trace')
        self.assertIn('SCHED_INSTRUMENTATION_IRQHANDLER',trace)
        self.assertNotIn('SCHED_INSTRUMENTATION_SYSCALL',trace)

    def test_correlated_intervals_and_public_allowlist(self):
        result = run(fixture())
        self.assertEqual(result['correlated_ticks']['ready_to_receive']['p50'],90)
        self.assertEqual(result['admission_ticks']['p50'],120)
        encoded = json.dumps(result)
        for private in ('address','sender_tid','receiver_tid','console','session','/Users/'):
            self.assertNotIn(private,encoded)
        self.assertFalse(result['physical_timing_qualified'])

    def test_rejects_plausible_but_invalid_evidence(self):
        for change in ('build','schedule','overflow','checksum','join','missing','duplicate','reorder','blocked','region','irq','wrap'):
            with self.subTest(change=change):
                rows = fixture()
                events = [r for r in rows if r['record']=='event']
                if change=='build': rows[0]['build_id']='b'*64
                if change=='schedule': rows[0]['sender_priority']=180
                if change=='overflow': rows[-1]['overflow']=True
                if change=='checksum': rows[-1]['checksum']+=1
                if change=='join': rows[-1]['threads_joined']=False
                if change=='missing': rows.remove(events[5]); rows[-1]['event_count']-=1
                if change=='duplicate': events[5]['kind']=10; events[5]['detail']=1; events[5]['tid']=20
                if change=='reorder': events[4]['ticks']=79
                if change=='blocked': events[2]['detail']=0
                if change=='region': rows[1]['region']=3
                if change=='irq': events[5]['kind']=5
                if change=='wrap': rows[4]['receive_return']=1<<31
                with self.assertRaises(ValueError): run(rows)

    def test_counter_wrap_and_balanced_interrupt(self):
        rows = fixture()
        for row in rows:
            for key in ('start','admission_return','receive_return','ticks'):
                if key in row: row[key]=(row[key]+analyze.MASK-115)&analyze.MASK
        result = run(rows)
        self.assertEqual(result['delivery_ticks']['p50'],100)
        events = [r for r in rows if r['record']=='event']
        index = rows.index(events[6])
        extra = [dict(record='event',ticks=12,kind=4,tid=21,detail=3),
                 dict(record='event',ticks=15,kind=5,tid=21,detail=3)]
        rows[index:index]=extra
        for i,row in enumerate(r for r in rows if r['record']=='event'): row['index']=i
        rows[-1]['event_count']+=2
        self.assertEqual(run(rows)['irq_entries']['max'],1)

    def test_duplicate_json_and_native_fault_are_rejected(self):
        text='\n'.join(analyze.PREFIX+json.dumps(r) for r in fixture())
        for bad in (text+'\nPANIC',text.replace('"samples": 1','"samples": 1, "samples": 1')):
            with self.assertRaises(ValueError):
                analyze.analyze(bad,{'build_id':'a'*64,'mode':'trace'},backend='crossbeam',policy='unequal',recording=True,samples=1,warmup=0)

    def test_build_identity_and_native_pins_are_enforced(self):
        pins=json.loads((Path(__file__).parent/'native-pins.json').read_text())
        self.assertEqual(set(pins['files']),set(capture.builder.PINS))
        record={'schema':'event-path-trace-build-v1','status':'complete','failure':None,'mode':'trace',
                **{k:{} for k in capture.builder.IDENTITY if k not in {'schema','mode'}}}
        record['build_id']=capture.builder.canonical({k:record[k] for k in capture.builder.IDENTITY})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory); (path/'build.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'native source pins'): capture.receipt(path)
            record['build_id']='0'*64; (path/'build.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'frozen build identity'): capture.receipt(path)


if __name__=='__main__': unittest.main()
