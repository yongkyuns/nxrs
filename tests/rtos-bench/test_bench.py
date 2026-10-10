import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from analyze import matched, thread_metric, compare
from build import HERE, basic_observable_source, config_identity
from footprint import compare as compare_footprint

class Validation(unittest.TestCase):
    def row(self):
        return dict(schema=1, backend='c-posix', case='queue-hot', iterations=100,
                    capacity=8, elapsed_ns=10000, clock_resolution_ns=1,
                    policy=1, priority=100, instrumentation='interval-only', valid=True)
    def test_false_success(self):
        row = self.row()
        for change in [dict(valid=False), dict(iterations=99), dict(elapsed_ns=0), dict(capacity=0),
                       dict(policy=-1), dict(schema=True), dict(valid=1)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                matched('RTBENCH ' + json.dumps(dict(row, **change)), 'c-posix', 'queue-hot', 100)
        for text in ['', 'RTBENCH ' + json.dumps(row) + '\nRTBENCH FAIL',
                     ('RTBENCH ' + json.dumps(row) + '\n') * 2]:
            with self.assertRaises(ValueError): matched(text, 'c-posix', 'queue-hot', 100)
    def tm(self, error=''):
        return ''.join(f'**** Thread-Metric Cooperative Scheduling Test **** Relative Time: {t}\n{error}Time Period Total:  100\n\n' for t in [30, 60])
    def test_heap_metrics_validate(self):
        row = self.row()
        row.update(heap_supported=True, heap_used_before=100, heap_used_setup=120,
                   heap_used_active=120, heap_used_after=100, heap_peak_before=150,
                   heap_peak_after=170, heap_largest_free_before=1000,
                   heap_largest_free_after=980)
        matched('RTBENCH ' + json.dumps(row), 'c-posix', 'queue-hot', 100)
        bad = copy.deepcopy(row)
        bad['heap_peak_after'] = 149
        with self.assertRaises(ValueError):
            matched('RTBENCH ' + json.dumps(bad), 'c-posix', 'queue-hot', 100)
    def test_original_counter_errors_survive(self):
        good = thread_metric(self.tm()); self.assertTrue(good['valid'])
        bad = thread_metric(self.tm('ERROR: Invalid counter value(s).\n'))
        self.assertFalse(bad['valid']); self.assertEqual(len(bad['windows'][0]['errors']), 1)
    def test_window_identity(self):
        for text in [self.tm().replace('60', '90'), self.tm() + self.tm(), self.tm().split('Relative Time: 60')[0]]:
            with self.assertRaises(ValueError): thread_metric(text)
    def test_comparison_guards(self):
        env = dict(runtime_kind='physical', board='test-board', clock_hz=80000000,
                   kernel_revision='revision', kernel_config_sha256='digest', build_profile='O2', measurement_session='A')
        left = dict(environment=env, result=self.row())
        right = copy.deepcopy(left); right['result']['backend'] = 'rust-posix'
        for result, setup, retained in [(left['result'], 20, 2), (right['result'], 28, 3)]:
            result.update(heap_supported=True, heap_used_before=100,
                          heap_used_setup=100 + setup, heap_used_active=100 + setup,
                          heap_used_after=100 + retained, heap_peak_before=150,
                          heap_peak_after=180, heap_largest_free_before=1000,
                          heap_largest_free_after=970)
        compared = compare(left, right)
        self.assertEqual(compared['right_over_left_throughput'], 1)
        self.assertEqual(compared['right_minus_left_heap_setup_bytes'], 8)
        self.assertEqual(compared['right_minus_left_heap_retained_bytes'], 1)
        r = copy.deepcopy(right); r['result']['clock_resolution_ns'] = 1000
        with self.assertRaisesRegex(ValueError, 'under-resolved'): compare(left, r)
        with self.assertRaisesRegex(ValueError, 'distinct backends'): compare(left, left)
        for field, value in [('runtime_kind', 'qemu'), ('clock_hz', None), ('kernel_config_sha256', 'other')]:
            r = copy.deepcopy(right); r['environment'][field] = value
            with self.assertRaises(ValueError): compare(left, r)
        for kind in ['qemu', 'host']:
            l, r = copy.deepcopy(left), copy.deepcopy(right)
            l['environment']['runtime_kind'] = r['environment']['runtime_kind'] = kind
            with self.assertRaises(ValueError): compare(l, r)
    def test_basic_observable_transform_is_narrow(self):
        source = ('prefix\nunsigned long   tm_basic_processing_counter;\n'
                  'volatile unsigned long   tm_basic_processing_array[1024];\nsuffix\n')
        changed = basic_observable_source(source)
        self.assertEqual(changed.count('volatile unsigned long   tm_basic_processing_counter;'), 1)
        self.assertEqual(changed.replace('volatile unsigned long   tm_basic_processing_counter;',
                                         'unsigned long   tm_basic_processing_counter;'), source)
        for bad in [source.replace('unsigned long   tm_basic_processing_counter;', ''),
                    source + 'unsigned long   tm_basic_processing_counter;\n']:
            with self.assertRaises(ValueError):
                basic_observable_source(bad)
    def test_minimal_footprint_comparison_requires_matched_kernel(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c, r = root/'c', root/'r'
            c.mkdir(); r.mkdir()
            c_config = 'CONFIG_RR_INTERVAL=10\nCONFIG_EXAMPLES_NXRS_BENCH=y\n'
            r_config = 'CONFIG_RR_INTERVAL=10\nCONFIG_EXAMPLES_NXRS_STD_APP=y\n'
            (c/'resolved.config').write_text(c_config)
            (r/'resolved.config').write_text(r_config)
            base = dict(text_bytes=100, data_bytes=10, bss_bytes=20,
                        flash_like_bytes=110, static_ram_bytes=30)
            (c/'image-size.json').write_text(json.dumps(base))
            rust = dict(text_bytes=130, data_bytes=12, bss_bytes=25,
                        flash_like_bytes=142, static_ram_bytes=37)
            (r/'image-size.json').write_text(json.dumps(rust))
            result = compare_footprint(c, r)
            self.assertEqual(result['rust_minus_c']['flash_like_bytes'], 32)
            self.assertEqual(result['rust_minus_c']['static_ram_bytes'], 7)
            (r/'resolved.config').write_text(
                'CONFIG_RR_INTERVAL=0\nCONFIG_EXAMPLES_NXRS_STD_APP=y\n')
            with self.assertRaisesRegex(ValueError, 'matched NuttX'):
                compare_footprint(c, r)
    def test_only_app_selection_normalized(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d)/'a', Path(d)/'b'
            a.write_text('CONFIG_RR_INTERVAL=10\nCONFIG_EXAMPLES_NXRS_STD_APP=y\n')
            b.write_text('CONFIG_RR_INTERVAL=10\nCONFIG_EXAMPLES_NXRS_BENCH=y\n')
            self.assertEqual(config_identity(a), config_identity(b))
            b.write_text('CONFIG_RR_INTERVAL=0\nCONFIG_EXAMPLES_NXRS_BENCH=y\n')
            self.assertNotEqual(config_identity(a), config_identity(b))
    @unittest.skipUnless(sys.platform == "linux", "native benchmark needs Linux POSIX message queues")
    def test_c_executable(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d)/'rt_c'
            subprocess.run(['gcc', '-std=c11', '-O2', '-fno-lto', '-Wall', '-Wextra', '-Werror',
                            '-pthread', str(HERE/'posix.c'), str(HERE/'c_main.c'), '-lrt', '-o', str(exe)], check=True)
            for case in ['semaphore-hot', 'heap128', 'queue-hot', 'yield-alone', 'semaphore-handoff']:
                for _ in range(2):
                    p = subprocess.run([exe, case, '1000'], capture_output=True, text=True, timeout=10, check=True)
                    matched(p.stdout, 'c-posix', case, 1000)
            for args in [[], ['missing', '100'], ['queue-hot', '0'], ['heap128', '-1'], ['queue-hot', '10000001']]:
                p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(p.returncode, 0); self.assertNotIn('RTBENCH {', p.stdout)
if __name__ == '__main__': unittest.main()
