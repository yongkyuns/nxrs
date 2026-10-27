"""Reference reads tolerate only the same bounded busy response as the client."""
import unittest
import shutil
import subprocess
from unittest import mock

import activity_reference as reference


class ActivityReferenceRequestTests(unittest.TestCase):
    def test_busy_get_retries_are_bounded_and_recover(self):
        send=mock.Mock(side_effect=[(429,b''),(429,b''),(200,b'page')])
        with mock.patch.object(reference.time,'sleep') as sleep:
            self.assertEqual(reference.request_with_busy_retry(send,'GET'),(200,b'page'))
        self.assertEqual(send.call_count,3)
        self.assertEqual(sleep.call_args_list,[mock.call(.15),mock.call(.3)])

    def test_persistent_busy_remains_a_failure_response(self):
        send=mock.Mock(return_value=(429,b'busy'))
        with mock.patch.object(reference.time,'sleep') as sleep:
            self.assertEqual(reference.request_with_busy_retry(send,'GET'),(429,b'busy'))
        self.assertEqual(send.call_count,3)
        self.assertEqual(sleep.call_count,2)

    def test_writes_authorization_and_service_failures_are_never_retried(self):
        for method,status in [('POST',429),('DELETE',429),('GET',401),('GET',403),('GET',503)]:
            with self.subTest(method=method,status=status), mock.patch.object(reference.time,'sleep') as sleep:
                send=mock.Mock(return_value=(status,b'failure'))
                self.assertEqual(reference.request_with_busy_retry(send,method),(status,b'failure'))
                send.assert_called_once(); sleep.assert_not_called()


class SourceRetirementTests(unittest.TestCase):
    def test_fast_retirement_between_stop_and_join_is_observed(self):
        state={'reconnects':4};publisher=mock.Mock()
        publisher.terminate.side_effect=lambda:state.update(reconnects=5)
        read=mock.Mock(side_effect=lambda:dict(state))
        # The old post-stop baseline is already 5 and misses this one transition.
        with mock.patch.object(reference.time,'sleep') as sleep:
            self.assertEqual(reference.retire_source(publisher,read),{'reconnects':5})
        self.assertEqual(read.call_count,2);publisher.wait.assert_called_once_with(timeout=10)
        sleep.assert_not_called()

    def test_no_retirement_still_fails_at_the_existing_deadline(self):
        publisher=mock.Mock()
        with mock.patch.object(reference.time,'monotonic',side_effect=[0,0,16]),mock.patch.object(reference.time,'sleep') as sleep:
            with self.assertRaisesRegex(AssertionError,'source loss is observed'):
                reference.retire_source(publisher,lambda:{'reconnects':4})
        sleep.assert_called_once_with(.2)


class ActivityStimulusTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'),'actual FFmpeg source qualification')
    def test_real_stimulus_repeats_motion_after_a_late_observer_can_admit(self):
        count,size=84,96*72
        result=subprocess.run([shutil.which('ffmpeg'),'-nostdin','-hide_banner','-loglevel','error',
            '-f','lavfi','-i',reference.motion_pattern(),'-vf','fps=2,scale=96:72',
            '-frames:v',str(count),'-threads','1','-pix_fmt','gray','-f','rawvideo','pipe:1'],
            capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,'actual synthetic source must execute')
        self.assertEqual(len(result.stdout),count*size,'source must outlive the old finite fixture')
        frame=lambda index:result.stdout[index*size:(index+1)*size]
        for index in [2,28,38,64,74]:
            self.assertEqual(len(set(frame(index))),1,'quiet phase must actually be static')
        for first,second in [(10,13),(46,49)]:
            self.assertGreater(len(set(frame(first))),16,'moving phase must contain spatial content')
            self.assertNotEqual(frame(first),frame(second),'motion must recur after delayed admission')



class RecorderReplacementTests(unittest.TestCase):
    def config(self):
        import hashlib
        digest=lambda token:hashlib.sha256(token.encode()).hexdigest()
        return dict(cameras=[dict(camera_id='camera')],credential_window=dict(not_before=10,expires_at=100),
                    viewers=[dict(viewer_id='denied',token_sha256=digest('a'*64))],
                    publishers=[dict(username='publisher',token_sha256=digest('b'*64))],
                    recorders=[dict(username='recorder',camera_id='camera',token_sha256=digest('c'*64))])

    def test_replacement_preserves_identity_scope_other_credentials_and_activation_window(self):
        import copy,hashlib
        config=self.config();before=copy.deepcopy(config)
        replacement=reference.recorder_replacement(config,'c'*64,'d'*64)
        expected=copy.deepcopy(before);expected['recorders'][0]['token_sha256']=hashlib.sha256(('d'*64).encode()).hexdigest()
        self.assertEqual(config,before)
        self.assertEqual(replacement,expected)

    def test_missing_reused_duplicate_or_malformed_key_cannot_replace_an_identity(self):
        for value in ['c'*64,'a'*64,'b'*64,'invalid',None]:
            with self.subTest(value=value),self.assertRaises(AssertionError):
                reference.recorder_replacement(self.config(),'c'*64,value)
        config=self.config();config['recorders']*=2
        with self.assertRaisesRegex(AssertionError,'one existing recorder'):
            reference.recorder_replacement(config,'c'*64,'d'*64)
        with self.assertRaisesRegex(AssertionError,'one existing recorder'):
            reference.recorder_replacement(self.config(),'0'*64,'d'*64)

    def test_group_retirement_requires_joined_owners_and_no_live_descendants(self):
        owners=[('observer',mock.Mock(pid=10)),('publisher',mock.Mock(pid=11)),('media',mock.Mock(pid=12))]
        for name,owner in owners:
            owner.poll.return_value=None;owner.wait.return_value=255 if name=='publisher' else 0
        with mock.patch.object(reference,'descendants',return_value={20}),mock.patch.object(reference,'process_ids',return_value=set()):
            reference.stop_rotation_group(owners)
        for name,owner in owners:
            owner.send_signal.assert_called_once_with(reference.signal.SIGINT if name=='media' else reference.signal.SIGTERM)
            owner.wait.assert_called_once_with(timeout=30)
        with mock.patch.object(reference,'descendants',return_value={20}),mock.patch.object(reference,'process_ids',return_value={20}):
            with self.assertRaisesRegex(AssertionError,'descendants joined'):
                reference.stop_rotation_group(owners)
        owners[0][1].wait.return_value=1
        with mock.patch.object(reference,'descendants',return_value=set()):
            with self.assertRaisesRegex(AssertionError,'owner joined successfully'):
                reference.stop_rotation_group(owners)

    def test_recorder_joins_while_its_publisher_control_and_media_are_still_live(self):
        # Ending the producer can retire a still-running consumer. The fixture
        # must request and join consumer shutdown before it removes that input.
        processes = {}
        class Process:
            def __init__(self, role, pid):
                self.role = role; self.pid = pid; self.status = None; self.joined = False
            def poll(self): return self.status
            def send_signal(self, signal):
                if self.role == 'publisher' and processes['observer'].status is None:
                    processes['observer'].status = 23
                if self.role == 'observer':
                    for role in ('publisher', 'control', 'media'):
                        if processes[role].status is not None:
                            self.status = 24; return
                self.status = 0
            def wait(self, timeout):
                self.joined = True
                return self.status
        for pid, role in enumerate(('publisher', 'observer', 'control', 'media'), 10):
            processes[role] = Process(role, pid)
        owners = list(processes.items())
        with mock.patch.object(reference, 'descendants', return_value=set()), \
             mock.patch.object(reference, 'process_ids', return_value=set()):
            reference.stop_rotation_group(owners)
        self.assertTrue(all(owner.joined and owner.status == 0 for owner in processes.values()))

    def test_preexisting_owner_exit_refuses_before_retiring_any_other_owner(self):
        observer = mock.Mock(pid=10); observer.poll.return_value = None
        observer.wait.return_value = 0
        publisher = mock.Mock(pid=11); publisher.poll.return_value = 1
        with mock.patch.object(reference, 'descendants', return_value=set()), \
             self.assertRaisesRegex(AssertionError, 'owner must be live'):
            reference.stop_rotation_group([('observer', observer), ('publisher', publisher)])
        observer.send_signal.assert_not_called()
        publisher.send_signal.assert_not_called()

    def test_unknown_or_duplicate_owner_identity_cannot_signal_a_process(self):
        owner = mock.Mock(pid=10)
        for owners in [[], [('unknown', owner)], [('observer', owner), ('observer', owner)]]:
            with self.subTest(owners=owners), self.assertRaisesRegex(AssertionError, 'identities are unique and known'):
                reference.stop_rotation_group(owners)
        owner.poll.assert_not_called(); owner.send_signal.assert_not_called()


class RecorderReplacementEvidenceTests(unittest.TestCase):
    def receipt(self):
        return dict(schema=1,scope='host-recorder-key-replacement',qualified=True,
                    new_decoded_frames=12,encrypted_storage_required=True,
                    physical_qualified=False,deployment_permitted=False,
                    **dict.fromkeys(reference.ROTATION_CHECKS,True))

    def test_complete_host_receipt_retains_only_the_executed_scope(self):
        value=self.receipt()
        self.assertEqual(reference.rotation_evidence(value,require_encrypted=True),value)
        value['encrypted_storage_required']=False
        reference.rotation_evidence(value)
        with self.assertRaisesRegex(AssertionError,'encrypted recorder replacement'):
            reference.rotation_evidence(value,require_encrypted=True)

    def test_qualified_flag_cannot_replace_controls_or_add_private_fields(self):
        for key in reference.ROTATION_CHECKS:
            value=self.receipt();value[key]=False
            with self.subTest(control=key),self.assertRaises(AssertionError):reference.rotation_evidence(value)
        for key,bad in [('schema',True),('new_decoded_frames',True),('new_decoded_frames',11),
                        ('new_decoded_frames',2**63),('physical_qualified',True),('deployment_permitted',True),
                        ('encrypted_storage_required','true')]:
            value=self.receipt();value[key]=bad
            with self.subTest(field=key),self.assertRaises(AssertionError):reference.rotation_evidence(value)
        value=self.receipt();value['private']='SYNTHETIC_PRIVATE_DATA'
        with self.assertRaisesRegex(AssertionError,'exact recorder replacement receipt'):
            reference.rotation_evidence(value)


class RecorderManagementConfigTests(unittest.TestCase):
    def test_strict_case_keeps_management_session_endpoint_and_loopback_tls(self):
        from pathlib import Path
        cfg=reference.recording_media_config(Path('/private/fixture'),auth=1,rtsp=2,management=3,web_port=4,udp_port=5,case='recorder-rotation')
        self.assertIs(cfg['api'],True);self.assertIs(cfg['webrtc'],True)
        self.assertIs(cfg['webrtcEncryption'],True)
        self.assertEqual(cfg['apiAddress'],'127.0.0.1:3')
        self.assertEqual(cfg['webrtcAddress'],'127.0.0.1:4')
        self.assertEqual(cfg['webrtcLocalUDPAddress'],'127.0.0.1:5')
        self.assertFalse(cfg['webrtcIPsFromInterfaces'])
        self.assertEqual(cfg['webrtcICEServers2'],[])
        self.assertEqual(cfg['webrtcAdditionalHosts'],['127.0.0.1'])


if __name__=='__main__':unittest.main()
