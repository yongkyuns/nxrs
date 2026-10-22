"""A crashed parent must not leave its decoder alive when the next owner starts."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class ActivitySupervisionTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix','Unix process-group supervision')
    def test_worker_crash_retires_its_child_before_restart_and_shutdown_joins_every_role(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            for name in ['control.json','activity.json']:
                (root/name).write_text('{}'); (root/name).chmod(0o600)
            control=root/'control'
            control.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(120)\n'); control.chmod(0o700)
            worker=root/'worker'
            worker.write_text('#!'+sys.executable+'\nimport json,subprocess,sys,time\nfrom pathlib import Path\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"])\n'
                'Path(sys.argv[1]).parent.joinpath("decoder.pid").write_text(str(p.pid))\ntime.sleep(120)\n')
            worker.chmod(0o700)
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'deploy/local_activity.py'),
                '--bundle',str(root),'--control',str(control),'--worker',str(worker)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            observed=[]
            def state():
                try: return json.loads((root/'runtime.json').read_text())
                except (OSError,ValueError): return {}
            def alive(pid):
                result=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True,timeout=3)
                return result.returncode==0 and result.stdout.strip() and not result.stdout.lstrip().startswith('Z')
            try:
                deadline=time.monotonic()+8
                while time.monotonic()<deadline and (not state().get('pids',{}).get('activity') or not (root/'decoder.pid').exists()): time.sleep(.05)
                first=state()['pids']; decoder=int((root/'decoder.pid').read_text()); observed=list(first.values())+[decoder]
                os.kill(first['activity'],signal.SIGKILL)
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    new=state().get('pids',{}).get('activity')
                    if new and new!=first['activity']: break
                    time.sleep(.05)
                self.assertNotEqual(new,first['activity'])
                self.assertFalse(alive(decoder),'old decoder must retire before new worker admission')
                observed+=list(state()['pids'].values())
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and int((root/'decoder.pid').read_text())==decoder: time.sleep(.05)
                observed.append(int((root/'decoder.pid').read_text()))
                process.terminate(); process.wait(timeout=40)
                self.assertEqual(process.returncode,0)
                self.assertEqual(state()['status'],'stopped')
                self.assertTrue(all(not alive(pid) for pid in observed))
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=40)
                for pid in observed:
                    if alive(pid):
                        try: os.kill(pid,signal.SIGKILL)
                        except ProcessLookupError: pass


if __name__=='__main__': unittest.main()
