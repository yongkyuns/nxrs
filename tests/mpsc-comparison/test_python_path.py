"""Regression: NuttX Kconfig must use the pinned venv despite /usr/bin cross tools."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from build import tool_path


class PythonPathTests(unittest.TestCase):
    def test_python_venv_precedes_system_compiler_directory(self):
        with tempfile.TemporaryDirectory(prefix='nxrs-mpsc-path-') as directory:
            root = Path(directory)
            venv = root / 'pinned venv' / 'bin'
            system = root / 'system-tools' / 'bin'
            zephyr = root / 'zephyr-tools' / 'bin'
            for directory in (venv, system, zephyr):
                directory.mkdir(parents=True)
            for path in (venv / 'python3', system / 'python3',
                         system / 'arm-none-eabi-gcc', zephyr / 'esptool'):
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            tools = {'python': str(venv / 'python'), 'path': [str(zephyr)]}
            path = tool_path(tools, str(system / 'arm-none-eabi-'), str(system))
            self.assertEqual(shutil.which('python3', path=path), str(venv / 'python3'))
            self.assertEqual(shutil.which('arm-none-eabi-gcc', path=path),
                             str(system / 'arm-none-eabi-gcc'))
            self.assertEqual(shutil.which('esptool', path=path), str(zephyr / 'esptool'))
            self.assertEqual(path.split(os.pathsep)[:3], [str(venv), str(system), str(zephyr)])


if __name__ == '__main__':
    unittest.main()
