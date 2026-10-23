"""Refuse coherent dependency drift in the actual-loader qualification graph."""
import unittest
import qualify_boot_hook as hook

BEFORE = '''version = 4
[[package]]
name = "bootsim"
version = "0.1.0"
dependencies = ["registry-component"]
[[package]]
name = "registry-component"
version = "1.2.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "frozen"
'''
AFTER = BEFORE.replace('["registry-component"]', '["registry-component", "nxrs-mcuboot-policy"]') + '''
[[package]]
name = "nxrs-firmware-image"
version = "0.1.0"
[[package]]
name = "nxrs-mcuboot-policy"
version = "0.1.0"
dependencies = ["nxrs-firmware-image"]
'''


class NativeGraph(unittest.TestCase):
    def test_exact_local_additions(self):
        hook.checked_graph(BEFORE, AFTER)

    def refused(self, after):
        with self.assertRaises(ValueError):
            hook.checked_graph(BEFORE, after)

    def test_registry_version_drift(self):
        self.refused(AFTER.replace('"1.2.3"', '"1.2.4"'))

    def test_registry_checksum_drift(self):
        self.refused(AFTER.replace('"frozen"', '"changed"'))

    def test_registry_source_substitution(self):
        self.refused(AFTER.replace('registry+https://github.com/rust-lang/crates.io-index', 'path+file:///fake'))

    def test_hidden_local_dependency(self):
        self.refused(AFTER.replace('dependencies = ["nxrs-firmware-image"]',
                                  'dependencies = ["nxrs-firmware-image", "registry-component"]'))

    def test_boot_callback_edge_missing(self):
        self.refused(AFTER.replace('["registry-component", "nxrs-mcuboot-policy"]', '["registry-component"]'))

    def test_extra_component(self):
        self.refused(AFTER + '\n[[package]]\nname="extra"\nversion="1.0.0"\n')

    def test_local_component_becomes_registry_package(self):
        self.refused(AFTER.replace('name = "nxrs-firmware-image"',
                                  'name = "nxrs-firmware-image"\nsource = "registry+fake"'))


class ExecutionOracle(unittest.TestCase):
    def output(self, enabled=True):
        required = hook.TESTS if enabled else (hook.TESTS[0],)
        return "\n".join(["test image::nxrs_policy_hook::" + name + " ... ok" for name in required] +
                         (["BOOT_POLICY_CASE positive passed", "BOOT_POLICY_CASE wrong-target passed"] if enabled else []))

    def test_both_execution_scopes(self):
        for enabled in (False, True):
            hook.verify_execution(self.output(enabled), ["positive", "wrong-target"], enabled)

    def test_missing_or_duplicated_case(self):
        for output in (self.output().replace("BOOT_POLICY_CASE wrong-target passed", ""),
                       self.output() + "\nBOOT_POLICY_CASE positive passed"):
            with self.assertRaises(ValueError):
                hook.verify_execution(output, ["positive", "wrong-target"], True)

    def test_reordered_case(self):
        with self.assertRaises(ValueError):
            hook.verify_execution(self.output(), ["wrong-target", "positive"], True)

    def test_ignored_missing_or_failed_test(self):
        for replacement in ("ignored", "FAILED", ""):
            with self.assertRaises(ValueError):
                hook.verify_execution(self.output().replace("... ok", "... " + replacement, 1),
                                      ["positive", "wrong-target"], True)

    def test_panic_cannot_supply_a_case_marker(self):
        with self.assertRaises(ValueError):
            hook.verify_execution(self.output() + "\npanicked at failure", ["positive", "wrong-target"], True)


if __name__ == "__main__":
    unittest.main()
