"""Exercise the actual shared timing gate, including repeated lifecycle."""
import ctypes
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent

class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        library = Path(cls.directory.name) / 'gate.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                        '-shared', '-fPIC', str(HERE / 'transport_gate.c'),
                        str(HERE / 'transport_gate_host_clock.c'), '-o', str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.nxrs_cq_gate_elapsed.restype = ctypes.c_uint32
    @classmethod
    def tearDownClass(cls): cls.directory.cleanup()

    def test_waits_for_all_peers_and_reuses_lifecycle(self):
        for count in (1, 20, 3):
            self.assertEqual(self.lib.nxrs_cq_gate_init(count), 0)
            self.assertEqual(self.lib.nxrs_cq_gate_init(count), -1)
            passed = []
            def peer(index):
                passed.append((index, self.lib.nxrs_cq_gate_arrive()))
            peers = [threading.Thread(target=peer, args=(i,)) for i in range(count)]
            for peer in peers: peer.start()
            time.sleep(0.005)
            self.assertEqual(passed, [])
            self.assertEqual(self.lib.nxrs_cq_gate_release(), 0)
            for peer in peers:
                peer.join(2)
                self.assertFalse(peer.is_alive())
            self.assertEqual(len(passed), count)
            self.assertTrue(all(result == 0 for _, result in passed))
            self.lib.nxrs_cq_gate_done()
            self.assertGreater(self.lib.nxrs_cq_gate_elapsed(), 0)
            self.assertEqual(self.lib.nxrs_cq_gate_destroy(), 0)
        self.assertEqual(self.lib.nxrs_cq_gate_init(0), -1)
        self.assertEqual(self.lib.nxrs_cq_gate_init(65), -1)
        self.assertEqual(self.lib.nxrs_cq_gate_destroy(), -1)

if __name__ == '__main__': unittest.main()
