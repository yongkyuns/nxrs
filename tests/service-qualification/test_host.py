"""Linux functional integration, never firmware RAM/speed evidence.

SQ_HOST_C and SQ_HOST_RUST select freshly built binaries. Without those
explicit paths these tests skip, instead of silently testing stale artifacts.
"""
import os
from pathlib import Path
import subprocess
import unittest

from results import parse


@unittest.skipUnless(os.environ.get("SQ_HOST_C") and os.environ.get("SQ_HOST_RUST"),
                     "set SQ_HOST_C and SQ_HOST_RUST to fresh Linux binaries")
class HostIntegration(unittest.TestCase):
    def programs(self):
        return [Path(os.environ[name]).resolve(strict=True) for name in ("SQ_HOST_C", "SQ_HOST_RUST")]

    def test_equal_work_and_ordered_delivery_under_backpressure(self):
        for program in self.programs():
            for services in (3, 20):
                with self.subTest(program=program.name, services=services):
                    output = subprocess.check_output([program, str(services), "1000", "100"], text=True, timeout=10)
                    result = parse(output, services=services, events=1000, source="messages")
                    self.assertEqual(result["result"]["received"], 1000)
                    self.assertEqual(result["done"]["status"], 0)

    def test_partial_startup_and_handler_failure_terminate(self):
        for program in self.programs():
            for failure in ("SQ_FAIL_LED_OPEN", "SQ_FAIL_LED_APPLY"):
                with self.subTest(program=program.name, failure=failure):
                    run = subprocess.run([program, "20", "100", "100"],
                                         env=dict(os.environ, **{failure: "1"}),
                                         capture_output=True, text=True, timeout=5)
                    self.assertEqual(run.returncode, 1)
                    self.assertIn("SQ_DONE status=1", run.stdout)

    def test_invalid_topology_is_rejected_before_startup(self):
        for program in self.programs():
            for count in ("2", "21"):
                with self.subTest(program=program.name, count=count):
                    run = subprocess.run([program, count], capture_output=True, timeout=5)
                    self.assertEqual(run.returncode, 2)
