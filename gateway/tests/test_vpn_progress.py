import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import vpn_progress as progress


class Receiver:
    def __init__(self):self.queue=asyncio.Queue();self.live=0;self.cancelled=0;self.maximum=0
    async def recv(self):
        self.live+=1;self.maximum=max(self.maximum,self.live)
        try:return await self.queue.get()
        except asyncio.CancelledError:self.cancelled+=1;raise
        finally:self.live-=1


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.output=self.root/'progress.json';self.stop=self.root/'stop'

    async def test_silence_then_fresh_frames_still_use_one_owned_receive_and_join_on_stop(self):
        receiver=Receiver();task=asyncio.create_task(progress.hold_frames(receiver,self.output,self.stop,5,0,seconds=2,poll=.01))
        await asyncio.sleep(.06)
        self.assertEqual(json.loads(self.output.read_bytes())['frames'],5)
        self.assertEqual(receiver.live,1);self.assertEqual(receiver.cancelled,0)
        for pts in range(1,4):receiver.queue.put_nowait(SimpleNamespace(width=320,height=240,pts=pts))
        for _ in range(30):
            if json.loads(self.output.read_bytes())['frames']==8:break
            await asyncio.sleep(.01)
        else:self.fail('fresh decoded frames not observed')
        self.stop.write_bytes(b'stop')
        self.assertEqual(await task,8);self.assertEqual(receiver.live,0)
        self.assertEqual(receiver.maximum,1);self.assertEqual(receiver.cancelled,1)
        self.assertEqual(self.output.stat().st_mode&0o777,0o600)
        self.assertFalse(self.output.with_name(self.output.name+'.pending').exists())

    async def test_deadline_joins_silent_receiver_without_restarting_reads(self):
        receiver=Receiver()
        with self.assertRaisesRegex(AssertionError,'deadline'):
            await progress.hold_frames(receiver,self.output,self.stop,5,0,seconds=.04,poll=.01)
        self.assertEqual(receiver.live,0);self.assertEqual(receiver.cancelled,1);self.assertEqual(receiver.maximum,1)

    async def test_external_cancellation_joins_pending_receiver(self):
        receiver=Receiver();task=asyncio.create_task(progress.hold_frames(receiver,self.output,self.stop,5,0,seconds=2,poll=.01))
        await asyncio.sleep(.02);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(receiver.live,0);self.assertEqual(receiver.cancelled,1)

    async def test_wrong_decoded_dimensions_refuse_and_join(self):
        receiver=Receiver();receiver.queue.put_nowait(SimpleNamespace(width=1,height=1))
        with self.assertRaisesRegex(AssertionError,'dimensions'):
            await progress.hold_frames(receiver,self.output,self.stop,5,0,seconds=2,poll=.01)
        self.assertEqual(receiver.live,0)
        self.assertEqual(json.loads(self.output.read_bytes())['frames'],5)

    async def test_receiver_timeout_is_a_failure_not_silent_network_progress(self):
        class Failed:
            async def recv(self):raise asyncio.TimeoutError('owned receive failure')
        with self.assertRaises(asyncio.TimeoutError):
            await progress.hold_frames(Failed(),self.output,self.stop,5,0,seconds=.05,poll=.01)
        self.assertEqual(json.loads(self.output.read_bytes())['frames'],5)

    async def test_duplicate_decoded_image_cannot_count_as_recovery(self):
        receiver=Receiver();receiver.queue.put_nowait(SimpleNamespace(width=320,height=240,pts=0))
        with self.assertRaisesRegex(AssertionError,'advancing decoded image'):
            await progress.hold_frames(receiver,self.output,self.stop,5,0,seconds=2,poll=.01)
        self.assertEqual(json.loads(self.output.read_bytes())['frames'],5);self.assertEqual(receiver.live,0)

    async def test_explicit_media_end_waits_for_controlled_cleanup_without_new_reads(self):
        class Ended:
            calls=0
            async def recv(self):self.calls+=1;raise EOFError()
        receiver=Ended();task=asyncio.create_task(progress.hold_frames(receiver,self.output,self.stop,5,0,
            seconds=2,poll=.01,end_errors=(EOFError,)))
        await asyncio.sleep(.04)
        self.assertEqual(receiver.calls,1);self.assertFalse(task.done())
        self.assertEqual(json.loads(self.output.read_bytes()),dict(schema=1,frames=5,ended=True))
        self.stop.write_bytes(b'stop');self.assertEqual(await task,5)

    async def test_progress_snapshots_are_private_bounded_regular_and_canonical(self):
        progress.publish(self.output,5);self.assertEqual(progress.read_progress(self.output),dict(schema=1,frames=5,ended=False))
        progress.publish(self.output,6,True);self.assertEqual(progress.read_progress(self.output),dict(schema=1,frames=6,ended=True))
        self.output.chmod(0o644)
        with self.assertRaises(AssertionError):progress.read_progress(self.output)
        self.output.chmod(0o600)
        alias=self.root/'alias';alias.symlink_to(self.output)
        with self.assertRaises(OSError):progress.read_progress(alias)
        fifo=self.root/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaises(AssertionError):progress.read_progress(fifo)
        for value in [dict(schema=True,frames=6,ended=False),dict(schema=1,frames=True,ended=False),
                      dict(schema=1,frames=4,ended=False),dict(schema=1,frames=6,ended='false'),
                      dict(schema=1,frames=6,ended=False,unknown=True)]:
            self.output.write_text(json.dumps(value))
            with self.assertRaises(AssertionError):progress.read_progress(self.output)

    async def test_existing_progress_or_stop_never_reuses_an_old_witness(self):
        for existing in [self.output,self.stop]:
            existing.write_bytes(b'old')
            with self.assertRaisesRegex(AssertionError,'new private'):
                await progress.hold_frames(Receiver(),self.output,self.stop,5,0,seconds=2,poll=.01)
            self.assertEqual(existing.read_bytes(),b'old');existing.unlink()


if __name__=='__main__':unittest.main()
