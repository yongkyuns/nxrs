"""Camera API organization regressions; behavior is exercised by Rust tests."""
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
CAMERA = ROOT / 'hal/camera'


def code(path):
    return re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)


class CameraOrganizationTests(unittest.TestCase):
    def test_public_api_is_explicit_and_does_not_select_an_os(self):
        source = code(CAMERA / 'src/lib.rs')
        self.assertNotRegex(source, r'\btarget_(?:os|arch|family|env|vendor)\b')
        self.assertNotRegex(source, r'pub\s+use\s+platform::\*')
        self.assertIn('pub fn open() -> Result<impl Camera, DeviceError>', source)
        self.assertRegex(source, r'if #\[cfg\(feature = "replay"\)\] \{\s*pub mod replay;\s*pub use replay::')
        self.assertIn('#![no_std]', source)
        self.assertIn('mod test_support;', source)

    def test_selector_and_backends_do_not_construct_or_reexport_replay(self):
        selector = code(CAMERA / 'src/platform/mod.rs')
        self.assertIn('use selected::open;', selector)
        self.assertNotRegex(selector, r'\b(?:fn|struct|impl|enum|trait)\b')
        self.assertNotIn('selected::*', selector)
        for path in (CAMERA / 'src/platform').rglob('*.rs'):
            with self.subTest(path=path):
                source = code(path)
                self.assertNotIn('crate::replay', source)
                self.assertNotIn('../replay/', source)
        self.assertFalse((CAMERA / 'src/replay/acquisition.rs').exists())
        self.assertFalse((CAMERA / 'src/replay/unavailable.rs').exists())

    def test_unavailable_type_stays_private(self):
        source = code(CAMERA / 'src/platform/unbound.rs')
        self.assertNotRegex(source, r'pub(?:\([^)]*\))?\s+struct')
        self.assertIn('pub fn open() -> Result<impl Camera, DeviceError>', source)
        self.assertIn('DeviceError::Unsupported', source)

    def test_replay_is_one_optional_cargo_functionality_boundary(self):
        manifest = tomllib.loads((CAMERA / 'Cargo.toml').read_text())
        self.assertEqual(manifest['features'], {'replay': []})
        for kind in ('example', 'test'):
            target = next(t for t in manifest[kind] if t['name'] == 'native_replay')
            self.assertEqual(target['required-features'], ['replay'])
        for path in (CAMERA / 'src/replay').rglob('*.rs'):
            with self.subTest(path=path):
                self.assertNotRegex(code(path), r'\bplatform\s*=|\b(?:hal_enabled|hal_mock|target_os|NXRS_PLATFORM)\b')

    def test_core_only_library_fixtures_do_not_enable_cli_replay(self):
        for name in ('portable', 'nuttx'):
            manifest = tomllib.loads((ROOT / 'tests' / name / 'Cargo.toml').read_text())
            self.assertIs(manifest['dependencies']['nxrs-applications']['default-features'], False)
        manifest = tomllib.loads((ROOT / 'app/nxrs/Cargo.toml').read_text())
        self.assertEqual(manifest['features']['replay'], [
            'nxrs-camera/replay', 'nxrs-storage/recording', 'nxrs-transport/udp',
        ])
        self.assertEqual(manifest['bin'][0]['required-features'], ['replay'])


if __name__ == '__main__':
    unittest.main()
