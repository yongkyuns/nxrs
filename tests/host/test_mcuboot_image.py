"""Geometry admission and real upstream signing, without accessing hardware."""
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mcuboot_image", ROOT / "tools/mcuboot_image.py")
IMAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGE)
GEOMETRY = {"flash-size": 0x100000, "primary-offset": 0x20000,
            "secondary-offset": 0x30000, "slot-size": 0x10000,
            "scratch-offset": 0x40000, "scratch-size": 0x1000,
            "header-size": 32, "write-align": 32, "erase-size": 4096}


class LayoutTests(unittest.TestCase):
    def test_image_class_requires_an_explicit_canonical_nonzero_uuid(self):
        valid = "2f24c1da-fce4-4d80-9a91-4bad54503e16"
        self.assertEqual(IMAGE.image_class({"image-class": valid}), valid)
        for value in (None, 42, "", "invalid", valid.upper(), valid.replace("-", ""),
                      "00000000-0000-0000-0000-000000000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                IMAGE.image_class({"image-class": value})

    def test_geometry_refuses_overlap_overflow_alignment_and_invalid_types(self):
        self.assertEqual(IMAGE.layout(GEOMETRY), GEOMETRY)
        # Other ports may place the scratch region first or start a slot at zero.
        for offsets in ((0, 0x10000, 0x20000), (0x20000, 0x30000, 0x1000)):
            portable = dict(GEOMETRY, **dict(zip(
                ("primary-offset", "secondary-offset", "scratch-offset"), offsets)))
            self.assertEqual(IMAGE.layout(portable), portable)
        for field, value in (("slot-size", True), ("slot-size", 0),
                             ("slot-size", 1 << 32), ("primary-offset", 0x20001),
                             ("secondary-offset", 0x20000), ("scratch-offset", 0x30000),
                             ("flash-size", 0x40000), ("header-size", 31),
                             ("header-size", 33), ("write-align", 3), ("erase-size", 3000)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                IMAGE.layout({**GEOMETRY, field: value})

    def test_product_build_requires_signing_inputs_before_building(self):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("NXRS_FIRMWARE_")}
        for args in ([], ["--out", "/private/tmp/nxrs-product-signing-guard"]):
            with self.subTest(args=args):
                result = subprocess.run(
                    ["bash", "platform/nuttx/home-cam/build.sh", *args],
                    cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("MCUboot build requires NXRS_FIRMWARE_SIGNING_KEY", result.stderr)

    def test_camera_default_and_explicit_qualification_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Capture the real frontend's backend invocation without building firmware.
            backend = Path(temporary) / "bash"
            backend.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            backend.chmod(0o755)
            environment = dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"])
            for script, args, platform, binary in (
                ("platform/nuttx/home-cam/build.sh", [], "esp32s3-camera-stream-startup", "home-cam"),
                ("tests/nuttx-std/stream/build.sh", ["--platform", "esp32s3-camera-stream"], "esp32s3-camera-stream", "nxrs-std-stream"),
                ("tests/nuttx-std/stream/build.sh", ["--tls", "--platform", "esp32s3-camera-tls"], "esp32s3-camera-tls", "tls-camera"),
            ):
                with self.subTest(args=args):
                    result = subprocess.run(
                        ["/bin/bash", script, *args],
                        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    values = result.stdout.splitlines()
                    self.assertEqual(values[values.index("--platform") + 1], platform)
                    self.assertEqual(values[values.index("--bin") + 1], binary)
            result = subprocess.run(
                ["/bin/bash", "tests/nuttx-std/stream/build.sh", "--tls"],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Qualification requires --platform", result.stderr)


@unittest.skipUnless(shutil.which("imgtool"), "install tools/mcuboot-requirements.txt for signature tests")
class SigningTests(unittest.TestCase):
    def test_real_signatures_wrong_root_corruption_oversize_and_atomic_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key, root, wrong = (directory / name for name in ("key.pem", "root.pem", "wrong.pem"))
            subprocess.run(["imgtool", "keygen", "-k", str(key), "-t", "ecdsa-p256"], check=True)
            subprocess.run(["imgtool", "keygen", "-k", str(wrong), "-t", "ecdsa-p256"], check=True)
            with root.open("w") as stream:
                subprocess.run(["imgtool", "getpub", "-k", str(key), "-e", "pem"],
                               stdout=stream, check=True)
            source, output = directory / "input.bin", directory / "signed.bin"
            source.write_bytes(bytes(range(256)) * 4)
            private = key.read_bytes()
            refused = directory / "private-root-build"
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools/build-mcuboot.py"), "--platform",
                 "esp32s3-camera-stream-mcuboot", "--root", str(key),
                 "--mcuboot-repo", str(directory), "--hal-repo", str(directory),
                 "--out", str(refused)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected EC P-256 PUBLIC key", result.stderr)
            self.assertFalse(refused.exists())
            IMAGE.sign(source, output, GEOMETRY, key, root, "1.2.3+4", 7)
            self.assertLess(output.stat().st_size, GEOMETRY["slot-size"])
            factory = directory / "factory.bin"
            IMAGE.sign(source, factory, GEOMETRY, key, root, "1.2.3+4", 7, confirm=True)
            self.assertEqual(factory.stat().st_size, GEOMETRY["slot-size"])
            # Independently inspect the documented header, then test actual verifier refusal.
            self.assertEqual(struct.unpack_from("<BBHI", output.read_bytes(), 20), (1, 2, 3, 4))
            good = output.read_bytes()
            for protected in (source, key, root):
                with self.assertRaises(ValueError):
                    IMAGE.sign(source, protected, GEOMETRY, key, root, "1.2.3+4", 7)
            for verification_root, data in ((wrong, source.read_bytes()), (root, b"x" * 0x10000)):
                source.write_bytes(data)
                with self.assertRaises(subprocess.CalledProcessError):
                    IMAGE.sign(source, output, GEOMETRY, key, verification_root, "1.2.3+4", 7)
                self.assertEqual(output.read_bytes(), good)
            corrupt = bytearray(good)
            corrupt[64] ^= 1
            output.write_bytes(corrupt)
            result = subprocess.run(["imgtool", "verify", "-k", str(root), str(output)],
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(key.read_bytes(), private)
            self.assertFalse(list(directory.glob(".mcuboot-*")))


if __name__ == "__main__":
    unittest.main()
