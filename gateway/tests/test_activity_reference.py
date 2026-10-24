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


if __name__=='__main__': unittest.main()
