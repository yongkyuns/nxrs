"""Recognize the pinned NSH prompt without changing retained console bytes."""


def nsh_prompt_ready(console: bytes | bytearray) -> bool:
    # NSH readline may append erase-to-end-of-line in the same read, or split
    # that escape across reads. Admit only the complete canonical suffix and
    # a prompt line; an echo, partial escape or later output is not completion.
    for suffix in (b"nsh> ", b"nsh> \x1b[K"):
        if console.endswith(suffix):
            start = len(console) - len(suffix)
            return start == 0 or console[start - 1] == ord("\n")
    return False
