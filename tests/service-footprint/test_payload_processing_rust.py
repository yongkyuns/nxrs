"""Run the identical independent golden/negative tests through Rust's C ABI."""
import ctypes
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest

import test_payload_processing_c as reference


class PayloadProcessingRustTests(reference.PayloadProcessingCTests):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('rustup'):
            raise unittest.SkipTest('pinned Rust host toolchain unavailable')
        super().setUpClass()
        library = Path(cls._build_dir.name) / 'libpayload_rust.so'
        subprocess.run(['rustup', 'run', '1.90.0', 'rustc', '--edition', '2021',
                        '--crate-type', 'cdylib', '-C', 'opt-level=z',
                        str(reference.HERE / 'src/payload_processing.rs'), '-o', str(library)],
                       check=True, capture_output=True, text=True)
        rust = ctypes.CDLL(str(library))
        aliases = {}
        for name in ('encode', 'parse', 'filter', 'filter_xyz'):
            function = getattr(rust, 'nxrs_rust_' + name)
            twin = getattr(cls.lib, 'nxrs_c_' + name)
            function.argtypes, function.restype = twin.argtypes, twin.restype
            aliases['nxrs_c_' + name] = function
        cls.rust_library = rust
        cls.lib = SimpleNamespace(**aliases)


if __name__ == '__main__': unittest.main()
