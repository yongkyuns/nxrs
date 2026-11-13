import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('website_build_api', Path(__file__).parents[1] / 'build_api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def write_receipt(root: Path, artifact: Path) -> dict:
    """Create a receipt for the small, explicitly synthetic rustdoc fixture."""
    catalog = api.load_catalog(root)
    docs = artifact / 'docs'
    files = api.files_below(docs)
    report = {
        'schema': api.SCHEMA, 'revision': 'a' * 40, 'dirty': False,
        'toolchain': '1.90.0', 'compiler': 'rustc 1.90.0\nhost: x86_64-unknown-linux-gnu',
        'rustdoc': 'rustdoc 1.90.0', 'host': 'x86_64-unknown-linux-gnu',
        'binding': 'unbound', 'features': catalog['features'],
        'packages': catalog['packages'], 'inputs': api.source_inputs(root, catalog),
        'files': {p.relative_to(docs).as_posix(): api.file_hash(p) for p in files},
        'output_bytes': sum(p.stat().st_size for p in files),
    }
    (artifact / api.MARKER).write_text(api.MARKER_TEXT)
    (artifact / 'manifest.json').write_text(json.dumps(report))
    return report


def make_api_fixture(root: Path, artifact: Path) -> dict:
    """Add two API crates and a verified artifact to an existing site test root."""
    sources = {
        'Cargo.toml': '[workspace]\nmembers = ["hal/demo", "service/helper", "tools/adapter", "shared/common"]\n'
                      '[workspace.dependencies]\nnxrs-common = { path = "shared/common" }\n',
        'Cargo.lock': '# synthetic locked dependency input\n',
        'rust-toolchain.toml': '[toolchain]\nchannel = "1.90.0"\n',
        'website/build_api.py': '# synthetic builder input\n',
        'hal/demo/Cargo.toml': '[package]\nname = "nxrs-demo"\nversion = "0.1.0"\n'
                               'build = "../../tools/adapter/cargo.rs"\n[lib]\nname = "demo"\n'
                               '[features]\nreplay = []\n[dependencies]\nnxrs-common = { workspace = true }\n'
                               '[build-dependencies]\nnxrs-adapter = { path = "../../tools/adapter" }\n'
                               '[target.\'cfg(target_os = "linux")\'.dependencies]\n'
                               'nxrs-helper = { path = "../../service/helper" }\n',
        'hal/demo/src/lib.rs': 'pub struct Demo;\n',
        'service/helper/Cargo.toml': '[package]\nname = "nxrs-helper"\nversion = "0.1.0"\n[lib]\nname = "helper"\n',
        'service/helper/src/lib.rs': 'pub struct Helper;\n',
        'tools/adapter/Cargo.toml': '[package]\nname = "nxrs-adapter"\nversion = "0.1.0"\n',
        'tools/adapter/src/lib.rs': 'pub fn configure() {}\n',
        'tools/adapter/cargo.rs': 'fn main() {}\n',
        'shared/common/Cargo.toml': '[package]\nname = "nxrs-common"\nversion = "0.1.0"\n',
        'shared/common/src/lib.rs': 'pub struct Common;\n',
        '.cargo/config.toml': '[alias]\nnxrs = "run -p nxrs-build --"\n',
    }
    for name, content in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    catalog = {
        'title': 'Rust API reference', 'features': ['nxrs-demo/replay'],
        'groups': [
            {'id': 'capabilities', 'title': 'Capability APIs', 'packages': [
                {'package': 'nxrs-demo', 'crate': 'demo', 'path': 'hal/demo',
                 'summary': 'A fixture capability.'}]},
            {'id': 'services', 'title': 'Reusable services', 'packages': [
                {'package': 'nxrs-helper', 'crate': 'helper', 'path': 'service/helper',
                 'summary': 'A fixture service.'}]},
        ],
    }
    (root / 'website/api.json').write_text(json.dumps(catalog))
    pages = {
        'demo/index.html': '<!doctype html><html><head><title>demo</title></head><body>'
                           '<main><h1>Crate demo</h1><a href="../helper/struct.Helper.html">Helper</a>'
                           '<a href="../src/demo/lib.rs.html#1-2">Source</a></main></body></html>',
        'helper/index.html': '<!doctype html><html><head><title>helper</title></head><body>'
                             '<main><h1>Crate helper</h1></main></body></html>',
        'helper/struct.Helper.html': '<!doctype html><html><head><title>Helper</title></head><body>'
                                    '<main><h1>Helper</h1></main></body></html>',
        'src/demo/lib.rs.html': '<!doctype html><html><head><title>lib.rs</title></head><body>'
                                '<main id="main-content"><pre><a id="1">1</a><a id="2">2</a>'
                                '</pre></main></body></html>',
        'search-index.js': '// synthetic search index\n',
        'crates.js': 'window.ALL_CRATES = ["demo", "helper"];\n',
        'static.files/rustdoc.css': '/* synthetic stylesheet */\n',
    }
    for name, content in pages.items():
        path = artifact / 'docs' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return write_receipt(root, artifact)


class ApiArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.artifact = Path(self.temp.name) / 'api'
        self.report = make_api_fixture(self.root, self.artifact)

    def test_catalog_and_receipt_keep_public_metadata(self):
        catalog = api.load_catalog(self.root)
        self.assertEqual([p['crate'] for p in catalog['packages']], ['demo', 'helper'])
        self.assertEqual(api.validate_artifact(self.root, self.artifact, 'a' * 40), self.report)

    def test_catalog_rejects_name_and_feature_mismatches(self):
        path = self.root / 'website/api.json'
        original = path.read_text()
        for key, value in [('crate', 'wrong'), ('package', 'nxrs-wrong'), ('path', '../outside')]:
            with self.subTest(key=key):
                catalog = json.loads(original)
                catalog['groups'][0]['packages'][0][key] = value
                path.write_text(json.dumps(catalog))
                with self.assertRaises(ValueError):
                    api.load_catalog(self.root)
        for features in [['nxrs-demo/missing'], ['nxrs-private/secret'], [{}], ['nxrs-demo/replay'] * 2]:
            with self.subTest(features=features):
                catalog = json.loads(original)
                catalog['features'] = features
                path.write_text(json.dumps(catalog))
                with self.assertRaises(ValueError):
                    api.load_catalog(self.root)

    def test_catalog_rejects_duplicate_packages(self):
        path = self.root / 'website/api.json'
        catalog = json.loads(path.read_text())
        catalog['groups'][0]['packages'] *= 2
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, 'unique'):
            api.load_catalog(self.root)

    def test_sources_build_adapter_and_cargo_config_invalidate_artifacts(self):
        for name in ['hal/demo/src/lib.rs', 'shared/common/src/lib.rs',
                     'tools/adapter/cargo.rs', 'Cargo.lock', '.cargo/config.toml',
                     'website/build_api.py']:
            with self.subTest(name=name):
                path = self.root / name
                previous = path.read_text()
                path.write_text(previous + '\n# changed input\n')
                with self.assertRaisesRegex(ValueError, 'stale'):
                    api.validate_artifact(self.root, self.artifact)
                path.write_text(previous)

    def test_unrelated_guide_edits_do_not_invalidate_api(self):
        (self.root / 'website/guide.md').write_text('# A new guide\n')
        api.validate_artifact(self.root, self.artifact)

    def test_changed_missing_and_extra_output_is_rejected(self):
        path = self.artifact / 'docs/demo/index.html'
        original = path.read_bytes()
        path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'files'):
            api.validate_artifact(self.root, self.artifact)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'files'):
            api.validate_artifact(self.root, self.artifact)
        path.write_bytes(original)
        (self.artifact / 'docs/unexpected.html').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'files'):
            api.validate_artifact(self.root, self.artifact)

    def test_required_crate_and_search_indexes_are_checked(self):
        for name in ['demo/index.html', 'search-index.js', 'crates.js']:
            with self.subTest(name=name):
                path = self.artifact / 'docs' / name
                original = path.read_bytes()
                path.unlink()
                write_receipt(self.root, self.artifact)
                with self.assertRaisesRegex(ValueError, 'missing'):
                    api.validate_artifact(self.root, self.artifact)
                path.write_bytes(original)
                write_receipt(self.root, self.artifact)

    def test_exact_revision_is_enforced_when_provided(self):
        with self.assertRaisesRegex(ValueError, 'revision'):
            api.validate_artifact(self.root, self.artifact, 'b' * 40)

    def test_linked_artifact_or_content_is_rejected(self):
        linked = Path(self.temp.name) / 'linked'
        linked.symlink_to(self.artifact, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'linked'):
            api.validate_artifact(self.root, linked)
        for path in [self.artifact / api.MARKER, self.artifact / 'docs/demo/index.html',
                     self.root / 'hal/demo/Cargo.toml']:
            with self.subTest(path=path):
                content = path.read_bytes()
                target = Path(self.temp.name) / 'link-target'
                target.write_bytes(content)
                path.unlink()
                path.symlink_to(target)
                with self.assertRaisesRegex(ValueError, 'linked'):
                    api.validate_artifact(self.root, self.artifact)
                path.unlink()
                path.write_bytes(content)

    def test_output_ownership_and_safe_location_precede_any_commands(self):
        unowned = Path(self.temp.name) / 'unowned'
        unowned.mkdir()
        sentinel = unowned / 'keep.txt'
        sentinel.write_text('keep')
        for output in [unowned, self.root, self.root.parent, self.root / 'target',
                       self.root / 'website/generated', self.root / 'hal/demo']:
            with self.subTest(output=output), mock.patch.object(api, 'capture') as capture:
                with self.assertRaises(ValueError):
                    api.build(self.root, output, self.root / 'target/cargo')
                capture.assert_not_called()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_linked_ownership_marker_does_not_authorize_replacement(self):
        path = Path(self.temp.name) / 'output'
        path.mkdir()
        (path / api.MARKER).symlink_to(self.artifact / api.MARKER)
        with self.assertRaisesRegex(ValueError, 'linked'):
            api.owned_output(self.root, path, api.MARKER)

    def test_artifact_and_cache_cannot_overlap(self):
        output = self.root / 'target/api'
        for cache in [output, output / 'cache', output.parent]:
            with self.subTest(cache=cache), mock.patch.object(api, 'capture') as capture:
                with self.assertRaises(ValueError):
                    api.build(self.root, output, cache)
                capture.assert_not_called()

    def test_host_environment_removes_platform_and_compiler_overrides(self):
        original = {
            'HOME': '/home/example', 'CARGO_HOME': '/cache/cargo', 'PATH': '/bin',
            'NXRS_PLATFORM': 'fixture', 'NXRS_RESOLVED_PROFILE': '/receipt',
            'NUTTX_INCLUDE_DIR': '/headers', 'CARGO_BUILD_TARGET': 'aarch64-unknown-none',
            'CARGO_CFG_PLATFORM': 'mock', 'CARGO_FEATURE_PICO_MODEL': '1',
            'RUSTFLAGS': '--cfg platform="mock"', 'RUSTDOCFLAGS': '--document-private-items',
            'RUSTC_WRAPPER': '/compiler-wrapper', 'RUSTUP_TOOLCHAIN': 'nightly',
            'CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS': '--cfg platform="mock"',
        }
        env = api.build_environment(original)
        self.assertEqual({k: env[k] for k in ['HOME', 'CARGO_HOME', 'PATH']},
                         {k: original[k] for k in ['HOME', 'CARGO_HOME', 'PATH']})
        self.assertFalse(any(k.startswith(('NXRS_', 'NUTTX_', 'CARGO_CFG_', 'CARGO_FEATURE_')) for k in env))
        self.assertNotIn('CARGO_BUILD_TARGET', env)
        self.assertNotIn('CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS', env)
        self.assertEqual(env['CARGO_ENCODED_RUSTFLAGS'], '')
        self.assertEqual(env['RUSTC_WRAPPER'], '')
        self.assertEqual(env['RUSTDOCFLAGS'], api.DOC_FLAGS)

    def test_cargo_config_cannot_restore_profile_or_compiler_flags(self):
        home = Path(self.temp.name) / 'cargo-home'
        home.mkdir()
        config = home / 'config.toml'
        for name in ['NXRS_PLATFORM', 'NXRS_RESOLVED_PROFILE', 'CARGO_FEATURE_PICO_MODEL',
                     'RUSTFLAGS', 'CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS']:
            with self.subTest(name=name):
                config.write_text(f'[env]\n{name} = "injected"\n')
                with self.assertRaisesRegex(ValueError, 'Cargo config'):
                    api.check_cargo_environment(self.root, {'CARGO_HOME': str(home)})


if __name__ == '__main__':
    unittest.main()
