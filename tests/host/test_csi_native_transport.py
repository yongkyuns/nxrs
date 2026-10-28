"""Exercise the actual SDK-witness word parser and host chunk acknowledgements."""
import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("native_transport", ROOT / "tests/csi-lab/native_replay.py")
N = importlib.util.module_from_spec(spec)
spec.loader.exec_module(N)


@unittest.skipUnless(shutil.which("cc"), "host C compiler required")
class NativeWords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.scratch.name) / "words"
        source = Path(cls.scratch.name) / "words.c"
        source.write_text('''#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define ESP_OK 0
int esp_radar_motion_dec_compute(unsigned,unsigned,const void *,unsigned,const void *,float *);
float esp_radar_motion_dec_wander_compute(const float *,const float *,unsigned);
#include ''' + json.dumps(str(ROOT / "tests/csi-radio/lab-replay.c")) + '''
int main(void) {
  float out[8];
  if (!lab_words(7,out,8)) return 3;
  for(unsigned i=0;i<8;i++){uint32_t bits;memcpy(&bits,out+i,4);printf("%08x",bits);}
  return 0;
}
''')
        subprocess.run([shutil.which("cc"), "-std=c11", "-O2", str(source), "-o", str(cls.binary)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_full_unsigned_and_nonfinite_bit_patterns_remain_exact(self):
        words = ["00000000", "ffffffff", "80000000", "7fc00001", "00000001", "3F800000", "deadbeef", "7f800000"]
        p = subprocess.run([str(self.binary)], input=" ".join(words[:4]) + "\n" + " ".join(words[4:]) + "\n", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "CSI_LAB_KERNEL_WORDS id=7 words=4\nCSI_LAB_KERNEL_WORDS id=7 words=8\n" + "".join(words).lower())

    def test_malformed_or_truncated_words_fail_without_ack_or_crash(self):
        valid = ["00000000"] * 4
        bad = ["f" * 32] + valid[1:]
        inputs = [" ".join(bad) + "\n", " ".join(valid) + " 00000000\n",
                  " ".join(valid[:-1]) + "\n", " ".join(valid),
                  "g0000000 " + " ".join(valid[1:]) + "\n", "f" * 200 + "\n"]
        for text in inputs:
            with self.subTest(text=text):
                p = subprocess.run([str(self.binary)], input=text, capture_output=True, text=True)
                self.assertEqual((p.returncode, p.stdout), (3, ""))


class ChunkAcknowledgements(unittest.TestCase):
    def test_next_chunk_waits_for_matching_identity_and_word_count(self):
        call = dict(kind="motion", id=7, cols=1, rows0=10, rows1=0,
                    input0_bits=list(range(10)), input1_bits=[])
        sent = []
        replies = iter(["CSI_LAB_KERNEL_WORDS ", "id=7 words=4\n", "CSI_LAB_KERNEL_WORDS ", "id=7 words=8\n", "CSI_LAB_KERNEL_WORDS ", "id=7 words=10\n"])
        waiting = []
        def read(marker, timeout):
            waiting.append(len(sent))
            return next(replies)
        N.send_call(call, sent.append, read)
        self.assertEqual(waiting, [2, 2, 3, 3, 4, 4])
        self.assertEqual([len(line.split()) for line in sent], [5, 4, 4, 2])
        for ack in ["id=8 words=4\n", "id=7 words=5\n", "id=7 words=4\nCSI_LAB_KERNEL_WORDS id=7 words=4\n"]:
            sent = []
            replies = iter(["CSI_LAB_KERNEL_WORDS ", ack])
            with self.subTest(ack=ack), self.assertRaises(ValueError):
                N.send_call(call, sent.append, lambda *_: next(replies))
            self.assertEqual(len(sent), 2)


class KernelImageAdmission(unittest.TestCase):
    def test_stale_or_unverified_uart_config_is_rejected_before_qemu(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            (build / "nuttx").mkdir()
            image = build / "nuttx/nuttx.merged.bin"
            image.write_bytes(b"fixture")
            config = build / "nuttx/.config"
            receipt = dict(mode="kernel", pins=json.loads((ROOT / "tests/csi-radio/pins.json").read_text()),
                           image_sha256=hashlib.sha256(image.read_bytes()).hexdigest())
            cases = [("CONFIG_ESP32S3_RX_FIFO_THRD=120\n", True),
                     ("CONFIG_ESP32S3_RX_FIFO_THRD=1\n", False)]
            for contents, matching_hash in cases:
                config.write_text(contents)
                receipt["config_sha256"] = hashlib.sha256(config.read_bytes()).hexdigest() if matching_hash else "0" * 64
                (build / "csi-build.json").write_text(json.dumps(receipt))
                with self.subTest(contents=contents), patch.object(N.subprocess, "Popen") as launch:
                    with self.assertRaisesRegex(ValueError, "byte-triggered UART"):
                        N.run(build, build / "qemu", build / "export", build / "trace")
                    launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
