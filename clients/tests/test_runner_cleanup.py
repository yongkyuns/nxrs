"""Failure controls for the real native-runner process tree, not production APIs."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from multicamera_reference import run_runner


class RunnerCleanupTests(unittest.TestCase):
    def test_runner_exit_status_is_preserved(self):
        self.assertEqual(asyncio.run(run_runner([sys.executable, '-c', 'raise SystemExit(7)'])), 7)

    def test_timeout_stops_descendant_that_ignores_term(self):
        with tempfile.TemporaryDirectory(prefix='nxrs-runner-control-') as directory:
            marker = Path(directory)/'escaped-child'
            ready = Path(directory)/'child-ready'
            child = ('import pathlib,signal,time; '
                     'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                     'pathlib.Path(__import__("sys").argv[2]).touch(); '
                     'time.sleep(2); pathlib.Path(__import__("sys").argv[1]).touch()')
            parent = ('import subprocess,sys,time; '
                      'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]); '
                      'time.sleep(60)')
            async def control():
                with self.assertRaises(asyncio.TimeoutError):
                    await run_runner([sys.executable,'-c',parent,child,marker,ready],timeout=1)
                self.assertTrue(ready.exists(), 'child did not reach its TERM-ignore barrier')
                await asyncio.sleep(2.1)
                self.assertFalse(marker.exists(), 'native child survived the fixture timeout')
            asyncio.run(control())


if __name__ == '__main__':
    unittest.main()
