"""Host prompt controls from the pinned NSH UART protocol."""

import unittest

from qemu_console import nsh_prompt_ready


class NshPromptAdmission(unittest.TestCase):
    def test_plain_and_erase_line_prompts_preserve_all_bytes(self):
        for prefix in (b"", b"NuttShell\r\n", b"NXRS_TIMED_WITNESS PASS\r\n"):
            for suffix in (b"nsh> ", b"nsh> \x1b[K"):
                for storage in (bytes, bytearray):
                    with self.subTest(prefix=prefix, suffix=suffix, storage=storage):
                        console = storage(prefix + suffix)
                        before = bytes(console)
                        self.assertTrue(nsh_prompt_ready(console))
                        self.assertEqual(console, before)

    def test_incomplete_erase_line_waits_for_its_final_byte(self):
        console = bytearray(b"NXRS_TIMED_WITNESS PASS\r\nnsh> ")
        self.assertTrue(nsh_prompt_ready(console))
        for byte in b"\x1b[":
            console.append(byte)
            self.assertFalse(nsh_prompt_ready(console))
        console.append(ord("K"))
        self.assertTrue(nsh_prompt_ready(console))

    def test_only_a_complete_final_prompt_line_is_admitted(self):
        for console in (
            b"", b"output only", b"nsh>", b"nsh>\t", b"value nsh> ",
            b"nsh> command", b"nsh> \r\nnew output", b"nsh> \r\n",
            b"nsh> \x1b", b"nsh> \x1b[", b"nsh> \x1b[0K",
            b"nsh> \x1b[Knot a prompt", b"nsh> \x1b[K\x1b[K",
        ):
            with self.subTest(console=console):
                self.assertFalse(nsh_prompt_ready(console))


if __name__ == "__main__":
    unittest.main()
