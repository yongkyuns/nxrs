"""A source cache must not carry configuration-dependent compiler output."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "tools/prepare-esp-hal-cache.sh"


class EspHalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nxrs-hal-cache-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cache = self.base / "source cache"
        self.destination = self.base / "build checkout"
        self.init_repo(self.cache)
        (self.cache / "driver.c").write_text(
            "int event_queue(void) { return HAS_HPWORK ? 1 : 0; }\n")
        (self.cache / "vendor.a").write_bytes(b"tracked vendor binary")
        (self.cache / ".gitignore").write_text("*.o\n")
        self.git(self.cache, "add", ".")
        self.git(self.cache, "commit", "-qm", "fixture")
        self.revision = self.git(self.cache, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def git(directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True,
                              capture_output=True, text=True)

    def init_repo(self, directory):
        directory.mkdir()
        self.git(directory, "init", "-q")
        self.git(directory, "config", "user.name", "Test Fixture")
        self.git(directory, "config", "user.email", "fixture@example.invalid")

    def prepare(self, revision=None, cache=None):
        return subprocess.run(["bash", str(PREPARE), str(cache or self.cache),
                               str(self.destination), revision or self.revision],
                              capture_output=True, text=True)

    def add_vendor_submodule(self):
        module = self.base / "module"
        self.init_repo(module)
        (module / "vendor.a").write_bytes(b"nested tracked binary")
        self.git(module, "add", ".")
        self.git(module, "commit", "-qm", "module")
        self.git(self.cache, "-c", "protocol.file.allow=always", "submodule",
                 "add", "-q", str(module), "components/vendor")
        self.git(self.cache, "commit", "-qam", "submodule")
        self.revision = self.git(self.cache, "rev-parse", "HEAD").stdout.strip()
        return self.cache / "components/vendor"

    def test_root_and_submodule_outputs_removed_without_touching_cache(self):
        self.add_vendor_submodule()
        artifacts = ("driver.o", ".depend", "components/vendor/driver.o")
        for name in artifacts:
            (self.cache / name).write_bytes(b"old configuration")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in artifacts:
            self.assertFalse((self.destination / name).exists(), name)
            self.assertEqual((self.cache / name).read_bytes(), b"old configuration")
        for name in ("vendor.a", "components/vendor/vendor.a"):
            self.assertEqual((self.destination / name).read_bytes(), (self.cache / name).read_bytes())

    @unittest.skipUnless(shutil.which("cc"), "native C compiler required")
    def test_config_dependent_object_is_recompiled(self):
        cached_object = self.cache / "driver.o"
        subprocess.run(["cc", "-DHAS_HPWORK=1", "-c", str(self.cache / "driver.c"),
                        "-o", str(cached_object)], check=True, capture_output=True)
        original = cached_object.read_bytes()
        main = self.base / "main.c"
        main.write_text("int event_queue(void); int main(void) { return event_queue(); }\n")
        executable = self.base / "probe"
        subprocess.run(["cc", str(main), str(cached_object), "-o", str(executable)], check=True)
        self.assertEqual(subprocess.run([str(executable)]).returncode, 1)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.destination / "driver.o").exists())
        subprocess.run(["cc", "-DHAS_HPWORK=0", str(main), str(self.destination / "driver.c"),
                        "-o", str(executable)], check=True)
        self.assertEqual(subprocess.run([str(executable)]).returncode, 0)
        self.assertEqual(cached_object.read_bytes(), original)

    def test_wrong_revision_rejected_before_copy(self):
        self.assertNotEqual(self.prepare("0" * 40).returncode, 0)
        self.assertFalse(self.destination.exists())

    def test_dirty_tracked_source_requires_patch(self):
        (self.cache / "driver.c").write_text("unrecorded source change\n")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertFalse(self.destination.exists())

    def test_parent_repository_is_not_accepted_as_cache(self):
        child = self.cache / "not-a-checkout"
        child.mkdir()
        self.assertNotEqual(self.prepare(cache=child).returncode, 0)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_is_preserved(self):
        self.destination.mkdir()
        marker = self.destination / "keep"
        marker.write_bytes(b"owned by caller")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertEqual(marker.read_bytes(), b"owned by caller")

    def test_submodule_cleanup_cannot_follow_metadata_back_to_cache(self):
        nested = self.add_vendor_submodule()
        directory = self.git(nested, "rev-parse", "--absolute-git-dir").stdout.strip()
        (nested / ".git").write_text(f"gitdir: {directory}\n")
        marker = nested / "keep.o"
        marker.write_bytes(b"original cache output")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertEqual(marker.read_bytes(), b"original cache output")


if __name__ == "__main__":
    unittest.main()
