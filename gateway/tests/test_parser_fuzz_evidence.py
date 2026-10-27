import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('parser_fuzz_runner',ROOT/'gateway/fuzz/run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class ParserFuzzEvidenceTests(unittest.TestCase):
    @staticmethod
    def stats(units=100000,rss=128,cov=250,features=400):
        return (f'#{units} DONE cov: {cov} ft: {features} corp: 4/30b\n'
                f'stat::number_of_executed_units: {units}\nstat::peak_rss_mb: {rss}\n').encode()

    def test_only_executed_bounded_coverage_counters_can_qualify(self):
        self.assertEqual(runner.statistics(self.stats()),dict(executed_units=100000,peak_rss_mb=128,coverage_edges=250,features=400))
        for raw in [b'',b'private-value',self.stats(units=1),self.stats(units=100001),self.stats(rss=1025),self.stats(cov=0),
                    self.stats(features=0),b'stat::number_of_executed_units: 100000\n',self.stats().replace(b'100000',b'private-value')]:
            with self.assertRaises(ValueError):runner.statistics(raw)

    def test_unrecognized_native_text_is_not_added_to_public_counter_fields(self):
        result=runner.statistics(self.stats()+b'private payload, source path and crash bytes\n')
        self.assertEqual(set(result),{'executed_units','peak_rss_mb','coverage_edges','features'})

    def test_completed_child_is_observed_and_reaped_without_group_pid_reuse(self):
        import os,sys
        with tempfile.TemporaryDirectory() as temporary:
            status,raw=runner.execute([sys.executable,'-c','print("owned-control")'],Path(temporary),
                'success',dict(os.environ),3)
        self.assertEqual(status,0);self.assertEqual(raw,b'owned-control\n')

    def test_resource_failure_retires_the_owned_process_before_return(self):
        import os,sys
        from unittest import mock
        processes=[];original=runner.subprocess.Popen
        def spawn(*args,**kwargs):
            child=original(*args,**kwargs);processes.append(child);return child
        with tempfile.TemporaryDirectory() as temporary,mock.patch.object(runner.subprocess,'Popen',side_effect=spawn):
            with self.assertRaises(ValueError):
                runner.execute([sys.executable,'-c','import time;time.sleep(30)'],Path(temporary),
                               'deadline',dict(os.environ),.1)
        self.assertTrue(processes and all(p.returncode is not None for p in processes))
        for process in processes:
            with self.assertRaises(ChildProcessError):os.waitpid(process.pid,os.WNOHANG)


if __name__=='__main__':unittest.main()
