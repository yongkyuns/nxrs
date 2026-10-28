from pathlib import Path
import tempfile
import unittest
import service_lifetime as life


class ServiceLifetimeTests(unittest.TestCase):
    def state(self):
        return dict(LoadState='loaded',ActiveState='inactive',SubState='dead',Result='success',
            MainPID='0',ControlPID='0',Job='0',ControlGroup='',ExecMainStartTimestampMonotonic='20')

    def test_main_pid_zero_does_not_join_prestart_queued_jobs_or_transitions(self):
        value=self.state();self.assertTrue(life.quiescent(value,'owned.service'))
        for fields in [dict(ControlPID='123'),dict(Job='45'),dict(ActiveState='activating'),
                       dict(ActiveState='deactivating'),dict(LoadState='not-found')]:
            with self.subTest(fields=fields):self.assertFalse(life.quiescent(dict(value,**fields),'owned.service'))

    def test_latched_handler_requires_a_fresh_completed_invocation(self):
        value=dict(self.state(),ActiveState='active',SubState='exited')
        self.assertFalse(life.quiescent(value,'owned.service'))
        self.assertTrue(life.handler_finished(value,'owned.service',19))
        self.assertFalse(life.handler_finished(value,'owned.service',20))
        for fields in [dict(SubState='running'),dict(Result='exit-code'),dict(ControlPID='8'),dict(Job='9')]:
            with self.subTest(fields=fields):self.assertFalse(life.handler_finished(dict(value,**fields),'owned.service',19))

    def test_nested_cgroup_processes_block_recovery_even_without_reported_pids(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);group=root/'system.slice/owned.service';child=group/'nested';child.mkdir(parents=True)
            (group/'cgroup.procs').write_text('');(child/'cgroup.procs').write_text('77\n')
            state=dict(self.state(),ControlGroup='/system.slice/owned.service')
            self.assertFalse(life.quiescent(state,'owned.service',cgroup_root=root))
            (child/'cgroup.procs').write_text('')
            self.assertTrue(life.quiescent(state,'owned.service',cgroup_root=root))
            for path in ['/','/system.slice/other.service','/system.slice/../owned.service','relative']:
                self.assertFalse(life.quiescent(dict(state,ControlGroup=path),'owned.service',cgroup_root=root))

    def test_state_snapshot_rejects_missing_duplicate_unknown_and_nonnumeric_fields(self):
        raw=''.join(k+'='+v+'\n' for k,v in self.state().items())
        self.assertEqual(life.parse(raw.encode()),self.state())
        for changed in [raw+'Job=0\n',raw+'Unknown=0\n',raw.replace('Job=0\n',''),raw.replace('ControlPID=0','ControlPID=unknown'),'x'*8193]:
            with self.subTest(raw=changed[:20]),self.assertRaises(ValueError):life.parse(changed)


if __name__=='__main__':unittest.main()
