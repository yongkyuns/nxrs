import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'tools/init_reference.py'
spec = importlib.util.spec_from_file_location('init_reference', MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InitializationTests(unittest.TestCase):
    def test_separate_generated_credentials_and_truthful_source(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'enrollment'
            path = module.initialize(root, 'https://media.example.test:8443')
            config = json.loads(path.read_text())
            viewer = (root/'viewer.token').read_text().strip()
            publisher = (root/'publisher.token').read_text().strip()
            self.assertEqual(len(viewer), 64)
            self.assertNotEqual(viewer, publisher)
            self.assertEqual(config['viewers'][0]['token_sha256'], hashlib.sha256(viewer.encode()).hexdigest())
            self.assertEqual(config['publishers'][0]['token_sha256'], hashlib.sha256(publisher.encode()).hexdigest())
            self.assertEqual(config['cameras'][0]['source'], 'synthetic')
            self.assertNotIn(viewer, path.read_text())
            self.assertNotIn(publisher, path.read_text())

    @unittest.skipUnless(os.name == 'posix', 'Unix filesystem permissions')
    def test_private_directory_and_files(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'enrollment'
            module.initialize(root, 'https://localhost:8889')
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for file in root.iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'enrollment'
            module.initialize(root, 'https://localhost:8889')
            before = (root/'viewer.token').read_bytes()
            with self.assertRaises(FileExistsError):
                module.initialize(root, 'https://localhost:8889')
            self.assertEqual(before, (root/'viewer.token').read_bytes())

    def test_rejects_untrusted_shapes_before_writing(self):
        with tempfile.TemporaryDirectory() as parent:
            for i, url in enumerate(['http://host', 'https://user:password@host', 'https://host/path',
                                     'https://host/?token=x', 'https://host/#fragment', 'https://host:0']):
                root = Path(parent) / str(i)
                with self.assertRaises(ValueError):
                    module.initialize(root, url)
                self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
