"""A crashed parent must not leave its decoder alive when the next owner starts."""
import json
import importlib.util
import os
from pathlib import Path
import signal
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


class ActivitySupervisionTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix','Unix process-group supervision')
    def test_kernel_exit_observation_keeps_the_group_leader_reserved_until_all_signals(self):
        spec=importlib.util.spec_from_file_location('owned_activity',Path(__file__).resolve().parents[1]/'deploy/local_activity.py')
        supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)
        process=subprocess.Popen([sys.executable,'-c','pass'],start_new_session=True)
        watch=supervisor.ExitWatch(process);original=os.killpg;observed=[]
        try:
            deadline=time.monotonic()+5
            while not watch.exited() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(watch.exited());self.assertIsNone(process.returncode)
            def checked(group,value):
                self.assertEqual(group,process.pid);self.assertIsNone(process.returncode)
                observed.append(value);return original(group,value)
            with patch.object(supervisor.os,'killpg',side_effect=checked):supervisor.retire(process,watch)
            self.assertEqual(observed,[signal.SIGTERM,signal.SIGKILL]);self.assertEqual(process.returncode,0)
        finally:
            if process.returncode is None:
                try:original(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)
            watch.close()

    @unittest.skipUnless(os.name=='posix','Unix process-group supervision')
    def test_a_live_child_permission_failure_is_never_accepted_as_retirement(self):
        spec=importlib.util.spec_from_file_location('live_activity',Path(__file__).resolve().parents[1]/'deploy/local_activity.py')
        supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True)
        watch=supervisor.ExitWatch(process);original=os.killpg
        try:
            with patch.object(supervisor.os,'killpg',side_effect=PermissionError):
                with self.assertRaises(PermissionError):supervisor.retire(process,watch)
            self.assertIsNone(process.returncode)
        finally:
            try:original(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=5);watch.close()

    @unittest.skipUnless(sys.platform=='darwin','Darwin zombie-only process group semantics')
    def test_darwin_permission_failure_requires_an_observed_reserved_exit(self):
        spec=importlib.util.spec_from_file_location('exited_activity',Path(__file__).resolve().parents[1]/'deploy/local_activity.py')
        supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)
        process=subprocess.Popen([sys.executable,'-c','pass'],start_new_session=True);watch=supervisor.ExitWatch(process)
        try:
            deadline=time.monotonic()+5
            while not watch.exited() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(watch.exited());self.assertIsNone(process.returncode)
            with patch.object(supervisor.os,'killpg',side_effect=PermissionError):supervisor.retire(process,watch)
            self.assertEqual(process.returncode,0)
        finally:
            if process.returncode is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)
            watch.close()

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
                'code="import time;time.sleep(120) # "+str(Path(sys.argv[1]).parent)\n'
                'p=subprocess.Popen([sys.executable,"-c",code])\n'
                'Path(sys.argv[1]).parent.joinpath("decoder.pid").write_text(str(p.pid))\ntime.sleep(120)\n')
            worker.chmod(0o700)
            errors=open(root/'supervisor.stderr','wb',opener=lambda p,f:os.open(p,f,0o600));self.addCleanup(errors.close)
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'deploy/local_activity.py'),
                '--bundle',str(root),'--control',str(control),'--worker',str(worker)],
                stdout=subprocess.DEVNULL,stderr=errors,start_new_session=True)
            observed=[]
            def state():
                try: return json.loads((root/'runtime.json').read_text())
                except (OSError,ValueError): return {}
            def alive(pid):
                result=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True,timeout=3)
                return result.returncode==0 and result.stdout.strip() and not result.stdout.lstrip().startswith('Z')
            def owned_alive(pid):
                result=subprocess.run(['ps','-ww','-p',str(pid),'-o','args='],capture_output=True,text=True,timeout=3)
                return result.returncode==0 and str(root) in result.stdout and alive(pid)
            def failure():
                raw=(root/'supervisor.stderr').read_bytes()[:65536].decode(errors='replace')
                classes=re.findall(r'^(PermissionError|ProcessLookupError|RuntimeError|OSError):',raw,re.M)
                return json.dumps(dict(supervisor_exit=process.poll(),status=state().get('status'),exception_classes=classes))
            try:
                deadline=time.monotonic()+8
                while time.monotonic()<deadline and (not state().get('pids',{}).get('activity') or not (root/'decoder.pid').exists()): time.sleep(.05)
                first=state()['pids']; decoder=int((root/'decoder.pid').read_text()); observed=list(first.values())+[decoder]
                self.assertTrue(owned_alive(first['activity']));self.assertTrue(owned_alive(decoder))
                os.kill(first['activity'],signal.SIGKILL)
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    self.assertIsNone(process.poll(),failure())
                    new=state().get('pids',{}).get('activity')
                    if new and new!=first['activity']: break
                    time.sleep(.05)
                self.assertIsNotNone(new,failure());self.assertNotEqual(new,first['activity'],failure())
                self.assertFalse(owned_alive(decoder),'old decoder must retire before new worker admission')
                observed+=list(state()['pids'].values())
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and int((root/'decoder.pid').read_text())==decoder: time.sleep(.05)
                observed.append(int((root/'decoder.pid').read_text()))
                process.terminate(); process.wait(timeout=40)
                self.assertEqual(process.returncode,0)
                self.assertEqual(state()['status'],'stopped')
                self.assertTrue(all(not owned_alive(pid) for pid in observed))
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=40)
                for pid in observed:
                    if owned_alive(pid):
                        try: os.kill(pid,signal.SIGKILL)
                        except ProcessLookupError: pass


if __name__=='__main__': unittest.main()
