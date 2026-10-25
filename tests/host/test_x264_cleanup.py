import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/gateway-media'))
import x264_cleanup as cleanup


class X264Cleanup(unittest.TestCase):
    def fixture(self,root):
        path=root/'encoder/encoder.c';path.parent.mkdir();before=b'owned synthetic constructor\n'+cleanup.BEFORE+b'\n';path.write_bytes(before)
        after=before.replace(cleanup.BEFORE,cleanup.AFTER,1)
        pin=dict(cleanup.recipe(),before_sha256=hashlib.sha256(before).hexdigest(),after_sha256=hashlib.sha256(after).hexdigest())
        return path,before,after,pin

    def test_exact_patch_preserves_other_bytes_and_refuses_reapplication(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path,before,after,pin=self.fixture(root)
            with patch.object(cleanup,'recipe',return_value=pin):
                self.assertEqual(cleanup.apply(root),dict(applied=True,**pin));self.assertEqual(path.read_bytes(),after)
                with self.assertRaisesRegex(ValueError,'exact-x264-cleanup-base'):cleanup.apply(root)
                self.assertEqual(path.read_bytes(),after)

    def test_changed_base_patch_or_expected_result_refuses_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path,before,after,pin=self.fixture(root)
            for name in ['before_sha256','after_sha256','patch_sha256']:
                invalid=dict(pin,**{name:'0'*64})
                with patch.object(cleanup,'recipe',return_value=invalid):
                    with self.assertRaises(ValueError):cleanup.apply(root)
                self.assertEqual(path.read_bytes(),before)
            destination=root/'outside';destination.write_bytes(before);path.unlink();path.symlink_to(destination)
            with patch.object(cleanup,'recipe',return_value=pin):
                with self.assertRaisesRegex(ValueError,'regular-x264-encoder-source'):cleanup.apply(root)
            self.assertEqual(destination.read_bytes(),before)

    def test_baseline_requires_actual_leak_marker_failure_status_and_complete_native_controls(self):
        marker=('X264_OPEN_CLEANUP '+json.dumps(cleanup.COUNTS)+'\n').encode()
        leak=b'ERROR: LeakSanitizer: detected memory leaks\nSUMMARY: AddressSanitizer: 166 byte(s) leaked in 3 allocation(s).\n'
        self.assertTrue(cleanup.observation(marker+leak,86,baseline=True)['leak_detected'])
        self.assertFalse(cleanup.observation(marker,0,baseline=False)['leak_detected'])
        for raw,status,baseline in [(marker,86,True),(marker+leak,0,True),(marker+leak,0,False),
                (marker+leak,86,False),(leak,86,True),(marker*2+leak,86,True),
                (marker+b'runtime error: invalid operation\n',0,False),
                (marker+leak+b'ERROR: AddressSanitizer: double-free\n',86,True)]:
            with self.subTest(status=status,baseline=baseline):
                with self.assertRaises(ValueError):cleanup.observation(raw,status,baseline=baseline)

    def test_personal_host_refuses_before_files_commands_or_network(self):
        import prepare
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);work=root/'absent';report=root/'absent.json'
            with patch.dict(cleanup.os.environ,{},clear=True),patch.object(prepare.native,'command',side_effect=AssertionError('command ran')),patch.object(prepare.urllib.request,'urlopen',side_effect=AssertionError('network ran')):
                with self.assertRaises(ValueError):cleanup.run(work,report)
            self.assertFalse(work.exists());self.assertFalse(report.exists())


if __name__=='__main__':unittest.main()
