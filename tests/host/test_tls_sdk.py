"""Source admission and archive rejection; no hardware or network access."""
import ctypes
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class TlsSdkTests(unittest.TestCase):
    def test_archive_is_verified_before_native_build_or_extraction(self):
        build = runpy.run_path(str(ROOT/'hal/tls/build-sdk.py'))['build']
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = work/'release.tar.bz2'
            with patch.dict(os.environ, NXRS_TLS_ARCHIVE=str(archive)):
                for content in [None, b'corrupt release']:
                    if content is not None:
                        archive.write_bytes(content)
                    with self.assertRaisesRegex(ValueError, 'Verified TLS source archive required'):
                        build(work/'out', 'x86_64-unknown-linux-gnu')
                    self.assertFalse((work/'out').exists())

    def test_nuttx_factory_rejects_unqualified_sources_and_uses_nonblocking(self):
        rp = {'BUILD_FLAT', 'ARCH_CHIP_RP23XX', 'RP23XX_RNG',
              'DEV_RANDOM', 'DEV_URANDOM_ARCH'}
        s3 = {'BUILD_FLAT', 'ARCH_CHIP_ESP32S3', 'ESP32S3_RNG',
              'DEV_RANDOM', 'ESPRESSIF_WIFI'}
        cases = [(rp, True),
                 (rp - {'RP23XX_RNG'}, False),
                 (rp | {'SMP'}, False),
                 (rp | {'DEV_URANDOM_RANDOM_POOL'}, False),
                 (s3, True)]
        cases.extend((s3 - {required}, False) for required in s3)
        cases.extend((s3 | {unsupported}, False)
                     for unsupported in ('SMP', 'PM', 'ESPRESSIF_AUTO_SLEEP',
                                         'ESP32S3_AUTO_SLEEP'))
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work/'nuttx').mkdir()
            for index, (config, expected) in enumerate(cases):
                defines = [f'#define CONFIG_{name} 1' for name in config]
                if 'RP23XX_RNG' in config:
                    defines.append('#define CONFIG_RP23XX_RNG_POLL_TIMEOUT 100000')
                (work/'nuttx/config.h').write_text('\n'.join(defines)+'\n')
                library = work/f'gate{index}.so'
                subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                                '-I'+str(work), str(ROOT/'hal/tls/src/platform/nuttx/ffi/rng.c'),
                                '-o', str(library)], check=True, capture_output=True)
                gate = ctypes.CDLL(str(library))
                gate.tls_rng_admitted.restype = ctypes.c_bool
                self.assertEqual(gate.tls_rng_admitted(), expected,
                                 f'configuration {sorted(config)}')
                self.assertEqual(gate.tls_entropy_open_flags(), os.O_NONBLOCK)
