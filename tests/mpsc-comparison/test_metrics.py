import copy,json,subprocess,unittest
from pathlib import Path
import metrics
ROOT=Path(__file__).resolve().parent/'rust'

class WorkAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['cargo','build','--quiet','--locked','-p','nxrs-mpsc-comparison','--bin','mpsc-comparison'],cwd=ROOT,check=True,timeout=60)
        cls.rows=metrics.records(cls.run_case('stream',4,16,8,128))
    @classmethod
    def run_case(cls,mode,n,payload,capacity,samples):
        return subprocess.run([str(ROOT/'target/debug/mpsc-comparison'),mode,'std',str(n),str(payload),str(capacity),str(samples)],capture_output=True,check=True,timeout=15).stdout
    def raw(self,change):
        rows=copy.deepcopy(self.rows);change(rows)
        return ('\n'.join('MPSC '+json.dumps(r) for r in rows)+'\nMPSC_DONE\n').encode()
    def test_actual_engine_modes_conserve_work(self):
        for mode,n,payload,cap,count in [('sustained',4,16,8,8192),('stream',1,64,64,128),('paced',4,16,8,16),('burst',4,16,64,128),('overload',4,16,8,128),('backpressure',4,64,8,128)]:
            with self.subTest(mode=mode):
                s=metrics.analyze(self.run_case(mode,n,payload,cap,count))
                self.assertEqual(s['offered'],s['delivered']+s['rejected'])
                self.assertFalse(s['native']);self.assertFalse(s['accounting_available'])
                if mode!='overload':self.assertEqual(s['delivered'],count)
    def test_missing_and_duplicate_work_are_refused(self):
        def missing(r):r.remove(next(x for x in r if x['record']=='sample'))
        def duplicate(r):
            samples=[x for x in r if x['record']=='sample'];samples[-1]['id']=samples[0]['id']
        for change in (missing,duplicate):
            with self.subTest(change=change.__name__),self.assertRaises(ValueError):metrics.analyze(self.raw(change))
    def test_fifo_and_stage_corruption_are_refused(self):
        def fifo(r):
            a,b=[x for x in r if x['record']=='sample' and x['id'] in (128,132)];a['order'],b['order']=b['order'],a['order']
        def stage(r):
            s=next(x for x in r if x['record']=='sample');s['finished']=(s['received']-1)&0xffffffff
        for change in (fifo,stage):
            with self.subTest(change=change.__name__),self.assertRaises(ValueError):metrics.analyze(self.raw(change))
    def test_partial_cpu_and_unjoined_result_are_refused(self):
        def cpu(r):next(x for x in r if x['record']=='participant')['cpu_ns']=1
        def joined(r):next(x for x in r if x['record']=='result')['joined']=False
        for change in (cpu,joined):
            with self.subTest(change=change.__name__),self.assertRaises(ValueError):metrics.analyze(self.raw(change))
    def test_host_cannot_claim_native_scheduling(self):
        def native(r):
            m=next(x for x in r if x['record']=='metadata');m['native']=True;m['runtime']='nuttx'
        with self.assertRaisesRegex(ValueError,'participant priority'):metrics.analyze(self.raw(native))
    def test_cold_producer_and_lost_message_are_refused(self):
        def cold(r):next(x for x in r if x['record']=='metadata')['warm_blocked_senders']=0
        def lost(r):next(x for x in r if x['record']=='sample')['flags']=3
        for change in (cold,lost):
            with self.subTest(change=change.__name__),self.assertRaises(ValueError):metrics.analyze(self.raw(change))

if __name__=='__main__':unittest.main()
