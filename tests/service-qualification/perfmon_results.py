"""Strict whole-run counter protocol; these are not per-event cache misses."""
import re

from results import _ANSI_ESCAPE

MODES = {"fetch": (4, 0x23), "all": (4, 0x1ff),
         "data": (3, 0x1fe), "instructions": (2, 0x8dff)}
PATTERN = re.compile(
    r"SQ_PM mode=(\w+) select0=(\d+) mask0=(\d+) value0=(\d+) "
    r"select1=(\d+) mask1=(\d+) value1=(\d+) overflow=(\d+)", re.ASCII)


def parse_perfmon(text, *, mode):
    rows = [line.strip() for line in _ANSI_ESCAPE.sub("", text).splitlines()
            if re.match(r"^\s*SQ_PM(?:\s|$)", line)]
    if len(rows) != 1 or mode not in MODES:
        raise ValueError("exactly one counter row and a supported mode required")
    match = PATTERN.fullmatch(rows[0])
    if match is None or match[1] != mode:
        raise ValueError("malformed counter row or wrong mode")
    keys = ("select0", "mask0", "value0", "select1", "mask1", "value1", "overflow")
    result = dict(zip(keys, map(int, match.groups()[1:])))
    if any(value > 0xffffffff for value in result.values()):
        raise ValueError("counter field exceeds uint32")
    if ((result["select0"], result["mask0"]) != MODES[mode] or
            (result["select1"], result["mask1"]) != (5, 32) or result["overflow"]):
        raise ValueError("counter selector mismatch or overflow")
    return dict(mode=mode, **result)
