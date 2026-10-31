"""Pinned native stop/ownership source regressions; no DMA timing qualification."""
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CAM = 'arch/xtensa/src/esp32s3/esp32s3_cam.c'
CAP = 'drivers/video/v4l2_cap.c'
PATCH = '0102-camera-stop-acknowledgement.patch'


def function(source, name):
    match = re.search(r'^static[^;\n]*\b' + name + r'\([^;]*?\)\n\{', source, re.M)
    if match is None:
        raise ValueError('missing native function: ' + name)
    end = source.index('\n}\n', match.end()) + 3
    return source[match.start():end]


class CameraStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='nxrs-camera-stop-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = directory = Path(cls.temporary.name)
        series = runpy.run_path(str(ROOT / 'tools/apply-nuttx-patches.py'))['SERIES']['nuttx']
        assert series.count(PATCH) == 1
        revision = subprocess.check_output(['git', 'rev-parse', ':external/nuttx'], cwd=ROOT, text=True).strip()
        def upstream(path):
            return subprocess.check_output(['git', '-C', str(ROOT / 'external/nuttx'),
                                            'show', f'{revision}:{path}'], text=True)
        for path in (CAM, CAP):
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(upstream(path))
        env = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(directory.parent)}
        for name in series:
            if name == PATCH:
                continue
            subprocess.run(['git', 'apply', '--include=' + CAM, '--include=' + CAP,
                            str(ROOT / 'upstream/nuttx/patches' / name)], cwd=directory,
                           env=env, check=True, capture_output=True)
        # The register definitions are the actual pinned target header, not
        # fixture-selected masks. Only its SoC address/include dependency is stubbed.
        header = upstream('arch/xtensa/src/esp32s3/hardware/esp32s3_dma.h')
        (directory / 'dma_registers.h').write_text(header.replace(
            '#include "esp32s3_soc.h"', '#define DR_REG_GDMA_BASE 0\n#define BIT(n) (1u << (n))'))
        cls.binaries = {}
        for patched in (False, True):
            if patched:
                subprocess.run(['git', 'apply', '--check', '--whitespace=error',
                                str(ROOT / 'upstream/nuttx/patches' / PATCH)], cwd=directory,
                               env=env, check=True, capture_output=True)
                subprocess.run(['git', 'apply', str(ROOT / 'upstream/nuttx/patches' / PATCH)],
                               cwd=directory, env=env, check=True, capture_output=True)
            for source, include, fixture, names in (
                (CAM, 'camera_stop', 'esp32s3_camera_stop',
                 ('cam_stop_dma', 'cam_prepare_frame', 'cam_interrupt',
                  'esp32s3_cam_stop_capture', 'esp32s3_cam_uninit')),
                (CAP, 'v4l2_stop', 'v4l2_camera_stop',
                 ('stop_capture', 'start_capture', 'change_capture_state', 'cleanup_resources',
                  'complete_capture', 'capture_reqbufs', 'capture_qbuf', 'capture_streamoff',
                  'capture_takepict_stop', 'capture_open', 'capture_close', 'capture_unlink')),
            ):
                actual = (directory / source).read_text()
                selected = [function(actual, name) for name in names
                            if name != 'cam_stop_dma' or patched]
                (directory / (include + '.inc')).write_text('\n'.join(selected))
                binary = directory / f'{fixture}-{patched}'
                subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', '-Wno-unused-function',
                                '-I', str(directory), str(ROOT / 'tests/host' / (fixture + '.c')),
                                '-o', str(binary)], check=True, capture_output=True, text=True)
                cls.binaries[fixture, patched] = binary

    def cases(self, fixture, cases, baseline_refuses=()):
        for case in cases:
            with self.subTest(case=case):
                result = subprocess.run([str(self.binaries[fixture, True]), str(case)],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for case in baseline_refuses:
            with self.subTest(baseline=case):
                # Baseline must compile and reach a failed behavioral assertion.
                result = subprocess.run([str(self.binaries[fixture, False]), str(case)],
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, 'baseline unexpectedly satisfied stop contract')
                self.assertIn('Assertion', result.stderr)

    def test_dma_acknowledgment_and_uninit_retention(self):
        self.cases('esp32s3_camera_stop', (0, 1, 2), baseline_refuses=(0, 1, 2))

    def test_isr_does_not_publish_or_rearm_pending_dma(self):
        self.cases('esp32s3_camera_stop', (3, 4), baseline_refuses=(3,))

    def test_async_stop_partial_start_and_queue_ownership(self):
        self.cases('v4l2_camera_stop', (0, 1, 4, 5, 6, 7), baseline_refuses=(0, 1, 4, 5, 6))

    def test_failed_consuming_close_retains_native_owners(self):
        self.cases('v4l2_camera_stop', (2, 3), baseline_refuses=(2, 3))


if __name__ == '__main__':
    unittest.main()
