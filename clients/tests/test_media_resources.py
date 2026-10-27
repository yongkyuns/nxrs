"""Fixture resource admission; these tests do not qualify a native decoder."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import multicamera_reference as reference


class MediaResourcesTests(unittest.TestCase):
    def executable(self,path):
        path.write_bytes(b'owned unexecuted unit-test resource');path.chmod(0o700)
        return path

    def test_explicit_encoder_and_isolation_are_independent_of_the_input_generator(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);publisher=self.executable(root/'nxrs-rtsp-publisher')
            system=self.executable(root/'fixture-ffmpeg');selected=self.executable(root/'selected-ffmpeg')
            sandbox=self.executable(root/'sandbox');args=SimpleNamespace(ingest=root/'ingest')
            with patch.object(reference.shutil,'which',return_value=str(system)) as lookup:
                self.assertEqual(reference.media_resources(args),(system.resolve(),publisher,None))
                lookup.assert_called_once_with('ffmpeg');lookup.reset_mock()
                args.ffmpeg=selected;args.media_sandbox=sandbox
                self.assertEqual(reference.media_resources(args),(selected.resolve(),publisher,sandbox.resolve()))
                lookup.assert_not_called()

    def test_missing_sdk_publisher_refuses_before_camera_or_private_fixture_allocation(self):
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(ingest=Path(temp)/'ingest')
            with patch.object(reference.RustCamera,'start',new_callable=AsyncMock) as camera, \
                    patch.object(reference.tempfile,'TemporaryDirectory') as allocate:
                with self.assertRaisesRegex(AssertionError,'SDK publisher resource required'):
                    asyncio.run(reference.exercise(args))
                camera.assert_not_called();allocate.assert_not_called()

    def test_unavailable_or_nonexecutable_encoder_and_sandbox_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.executable(root/'nxrs-rtsp-publisher');encoder=self.executable(root/'encoder')
            args=SimpleNamespace(ingest=root/'ingest',ffmpeg=root)
            with self.assertRaisesRegex(AssertionError,'executable encoder resource required'):reference.media_resources(args)
            args.ffmpeg=encoder;encoder.chmod(0o600)
            with self.assertRaisesRegex(AssertionError,'executable encoder resource required'):reference.media_resources(args)
            encoder.chmod(0o700);args.media_sandbox=root/'missing'
            with self.assertRaisesRegex(AssertionError,'executable media sandbox resource required'):reference.media_resources(args)
            args.ffmpeg=None;args.media_sandbox=None
            with patch.object(reference.shutil,'which',return_value=None):
                with self.assertRaisesRegex(AssertionError,'encoder resource required'):reference.media_resources(args)


if __name__=='__main__':unittest.main()
