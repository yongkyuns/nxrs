"""Negative controls for frozen sources and actual compiler-package binding."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import build
import generate


class BuildEvidenceTests(unittest.TestCase):
    def package_fixture(self, root):
        (root / "bin").mkdir()
        (root / "lib").mkdir()
        (root / "bin/rustc").write_bytes(b"compiler")
        driver = root / "lib/librustc_driver-test.so"
        driver.write_bytes(b"driver")
        inventory = {"core/lib.rs": "a" * 64}
        ledger = {"patches": []}
        package = dict(
            std_source_inventory=[dict(path=k, sha256=v) for k, v in inventory.items()],
            compiler_binaries_sha256={"rustc": build.digest(root / "bin/rustc")},
            rustc_driver_library_sha256={driver.name: build.digest(driver)}, patch_ledger=ledger)
        proof = root / "proof.json"
        proof.write_text(json.dumps(package))
        return inventory, ledger, proof, driver

    def test_generated_artifact_and_source_changes_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="aq-build-") as folder:
            generated = Path(folder)/"generated"
            generate.generate(generated, False, ["u32_wrapping_add"])
            self.assertTrue(build.validate_generated(generated))
            path = generated/"kernels.c"
            original = path.read_bytes()
            path.write_bytes(original+b"\n")
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                build.validate_generated(generated)
            path.write_bytes(original)
            proof = json.loads((generated/"coverage.json").read_text())
            proof["sources"]["driver.c"]="0"*64
            (generated/"coverage.json").write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError,"source changed"):
                build.validate_generated(generated)

    def test_ledger_alone_cannot_claim_a_different_compiler(self):
        with tempfile.TemporaryDirectory(prefix="aq-package-") as folder:
            root = Path(folder)
            inventory, ledger, proof, driver = self.package_fixture(root)
            build.validate_compiler(root,inventory,ledger,proof)
            with self.assertRaisesRegex(ValueError,"snapshot/patch ledger"):
                build.validate_compiler(root,inventory,{"patches":["unqualified"]},proof)
            driver.write_bytes(b"different compiler")
            with self.assertRaisesRegex(ValueError,"compiler driver"):
                build.validate_compiler(root,inventory,ledger,proof)

    def test_optional_std_ledger_is_validated_against_the_actual_library(self):
        with tempfile.TemporaryDirectory(prefix="aq-std-package-") as folder:
            root = Path(folder)
            inventory, ledger, proof, _ = self.package_fixture(root)
            package = json.loads(proof.read_text())
            std_ledger = {"proposal": "fixture"}
            package["std_proposal_ledger"] = std_ledger
            proof.write_text(json.dumps(package))
            validator = Mock()
            with patch.object(build, "load", return_value=validator):
                build.validate_compiler(root, inventory, ledger, proof)
                validator.validate_std_proposal.assert_called_once_with(
                    root / "lib/rustlib/src/rust/library", std_ledger)
                validator.validate_std_proposal.side_effect = ValueError("std patch differs")
                with self.assertRaisesRegex(ValueError, "std patch differs"):
                    build.validate_compiler(root, inventory, ledger, proof)


if __name__ == "__main__":
    unittest.main()
