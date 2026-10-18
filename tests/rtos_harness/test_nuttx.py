"""Identity checks must work without a greeting and reject prompt-only boots."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.nuttx import verify_identity


class NuttxIdentityTests(unittest.TestCase):
    def test_fresh_uname_without_boot_greeting(self):
        command = Mock(return_value="uname\r\nNuttX\r\nnsh> ")
        self.assertEqual(verify_identity(command), command.return_value)
        command.assert_called_once_with("uname")

    def test_banner_prompt_echo_and_other_os_are_not_a_response(self):
        for response in ("nsh> ", "uname\r\nnsh> ",
                         "NuttShell (NSH)\r\nnsh> ", "uname\nLinux\nnsh> ",
                         "NuttX\nNuttX\nnsh> ", "text NuttX\nnsh> "):
            with self.subTest(response=response), self.assertRaises(ValueError):
                verify_identity(Mock(return_value=response))

    def test_transport_failure_is_not_hidden(self):
        with self.assertRaises(TimeoutError):
            verify_identity(Mock(side_effect=TimeoutError))


if __name__ == "__main__":
    unittest.main()
