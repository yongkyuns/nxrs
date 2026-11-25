"""Boot framing shared by the physical NuttX shell captures."""
import re

BOOT_MARKERS = re.compile(
    r"(?m)^(?:(?P<loader>\*\*\* Booting NuttX \*\*\*)|(?P<shell>NuttShell \(NSH\)(?: [^\r\n]*)?))\r?$"
)


def fresh_shell_boot(text):
    markers = tuple(match.lastgroup for match in BOOT_MARKERS.finditer(text))
    return markers in (("loader",), ("shell",), ("loader", "shell"))
