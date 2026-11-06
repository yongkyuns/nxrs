"""Cargo artifact evidence must keep target dependencies separate from host tools."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'tools/check-hal-isolation.py'
SPEC = importlib.util.spec_from_file_location('hal_isolation', SOURCE)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class HalIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.target = 'fixture-target'
        self.names = {'hal': 'nxrs-camera', 'common': 'nxrs-shared',
                      'build': 'nxrs-build-support', 'registry': 'serde',
                      'driver': 'unexpected-driver', 'macro': 'derive',
                      'dev': 'test-only'}
        self.expected = {'hal', 'common'}

        def edge(package, kind=None):
            return {'pkg': package, 'dep_kinds': [{'kind': kind}]}

        edges = {'hal': [edge('common'), edge('build', 'build'), edge('macro'), edge('dev', 'dev')],
                 'build': [edge('registry'), edge('driver'), edge('dev', 'dev')],
                 'macro': [edge('registry')]}
        self.metadata = {
            'packages': [{'id': identity, 'name': name,
                          'targets': [{'kind': ['proc-macro' if identity == 'macro' else 'lib']}]}
                         for identity, name in self.names.items()],
            'resolve': {'nodes': [{'id': identity, 'deps': edges.get(identity, [])} for identity in self.names]},
        }
        self.host = CHECK.host_dependencies(self.metadata, self.expected)

    def artifact(self, identity, scope='target', kind='lib'):
        directory = self.directory / self.target if scope == 'target' else self.directory
        return {'reason': 'compiler-artifact', 'package_id': identity,
                'target': {'kind': [kind]},
                'filenames': [str(directory / 'debug/deps' / (identity + '.rlib'))]}

    def check(self, artifacts):
        text = '\n'.join(json.dumps(record) for record in
                         [*artifacts, {'reason': 'build-finished', 'success': True}])
        return CHECK.check(text, self.names, self.expected, self.host, self.directory, self.target)

    def valid_artifacts(self):
        return [self.artifact('hal'), self.artifact('common'),
                self.artifact('hal', 'host', 'custom-build'),
                self.artifact('build', 'host'), self.artifact('registry', 'host'),
                self.artifact('driver', 'host'), self.artifact('macro', 'host', 'proc-macro')]

    def test_host_allowance_follows_cargo_build_and_macro_edges_not_dev_or_target_edges(self):
        self.assertEqual(self.host, {'build', 'registry', 'driver', 'macro'})

    def test_build_controller_is_rejected_even_as_a_declared_transitive_host_dependency(self):
        self.metadata['packages'].append({'id': 'controller', 'name': 'nxrs-build',
                                          'targets': [{'kind': ['lib']}]})
        nodes = self.metadata['resolve']['nodes']
        nodes.append({'id': 'controller', 'deps': []})
        build = next(node for node in nodes if node['id'] == 'build')
        build['deps'].append({'pkg': 'controller', 'dep_kinds': [{'kind': None}]})
        with self.assertRaisesRegex(AssertionError, 'must not compile the build controller'):
            CHECK.host_dependencies(self.metadata, self.expected)

    def test_observed_registry_host_artifacts_are_recorded(self):
        compiled = self.check(self.valid_artifacts())
        self.assertEqual(compiled['target'], self.expected)
        self.assertEqual(compiled['host'], self.host)
        self.assertEqual(compiled['build_scripts'], {'hal'})

    def test_registry_target_contamination_is_rejected_even_if_package_is_a_host_dependency(self):
        with self.assertRaisesRegex(AssertionError, 'target dependency set'):
            self.check([*self.valid_artifacts(), self.artifact('driver')])

    def test_package_can_be_observed_in_both_host_and_target_contexts(self):
        # A reviewed normal dependency can also be used by a build tool. Its
        # host artifact must not remove the target artifact from the evidence.
        self.host.add('common')
        compiled = self.check([*self.valid_artifacts(), self.artifact('common', 'host')])
        self.assertIn('common', compiled['target'])
        self.assertIn('common', compiled['host'])

    def test_unknown_and_dev_only_host_artifacts_are_not_silently_accepted(self):
        for identity, scope in [('unknown', 'target'), ('unknown', 'host'), ('dev', 'host')]:
            with self.subTest(identity=identity, scope=scope), self.assertRaises(AssertionError):
                self.check([*self.valid_artifacts(), self.artifact(identity, scope)])

    def test_missing_target_or_unrelated_build_script_fails(self):
        with self.assertRaisesRegex(AssertionError, 'target dependency set'):
            self.check([self.artifact('hal'), self.artifact('common', 'host')])
        with self.assertRaisesRegex(AssertionError, 'unexpected build scripts'):
            self.check([*self.valid_artifacts(), self.artifact('dev', 'host', 'custom-build')])

    def test_artifact_paths_and_host_only_kinds_are_verified(self):
        for artifact in [self.artifact('macro', kind='proc-macro'),
                         self.artifact('hal', kind='custom-build')]:
            with self.subTest(kind=artifact['target']['kind']), self.assertRaisesRegex(AssertionError, 'host-only'):
                self.check([*self.valid_artifacts(), artifact])
        artifact = self.artifact('registry', 'host')
        artifact['filenames'].extend(self.artifact('registry')['filenames'])
        with self.assertRaisesRegex(AssertionError, 'mixed host/target'):
            self.check([*self.valid_artifacts(), artifact])
        artifact['filenames'] = [str(self.directory.parent / 'outside.rlib')]
        with self.assertRaisesRegex(AssertionError, 'outside the audited output'):
            self.check([*self.valid_artifacts(), artifact])


if __name__ == '__main__':
    unittest.main()
