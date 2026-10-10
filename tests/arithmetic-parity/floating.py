"""Floating point parity kernels and an independent result oracle.

The input ABI carries each operand in ``a/b/c``; ``ah/bh/ch`` are the upper
64-bit halves reserved for 128-bit integer operands and are zero here. Every
kernel writes the result bits to ``lo`` and clears the
other result fields. Rational operations use exact arithmetic. Transcendental
operations use mpmath at 100 decimal digits before IEEE rounding. mpmath is a
required test-host dependency (the comparison venv pins version 1.3.0).
"""

from __future__ import annotations

from fractions import Fraction
import random
from typing import Any

import mpmath


_OPS = (
    ("add", "+"),
    ("sub", "-"),
    ("mul", "*"),
    ("div", "/"),
    ("rem", "%"),
    ("recip", "recip"),
    ("sqrt", "sqrt"),
    ("fma", "fma"),
    ("round", "round"),
    ("round_ties_even", "round_ties_even"),
    ("floor", "floor"),
    ("ceil", "ceil"),
    ("trunc", "trunc"),
    ("fract", "fract"),
    ("abs", "abs"),
    ("neg", "neg"),
    ("min", "min"),
    ("max", "max"),
    ("copysign", "copysign"),
    ("powf", "powf"), ("exp", "exp"), ("exp2", "exp2"),
    ("ln", "ln"), ("log2", "log2"), ("log10", "log10"),
    ("ln_1p", "ln_1p"), ("exp_m1", "exp_m1"),
    ("sin", "sin"), ("cos", "cos"), ("tan", "tan"),
    ("asin", "asin"), ("acos", "acos"), ("atan", "atan"),
    ("atan2", "atan2"), ("sinh", "sinh"), ("cosh", "cosh"),
    ("tanh", "tanh"), ("asinh", "asinh"), ("acosh", "acosh"),
    ("atanh", "atanh"), ("cbrt", "cbrt"), ("hypot", "hypot"),
)

_LIBM_OPS = frozenset((
    "powf", "exp", "exp2", "ln", "log2", "log10", "ln_1p", "exp_m1",
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh",
    "cosh", "tanh", "asinh", "acosh", "atanh", "cbrt", "hypot",
))

DELEGATED_OPERATIONS = {
    "powi": "provided by conversions.py with bounded integer exponents",
    "conversions": "float-to-int and int-to-float cases are provided by conversions.py",
}


def operations() -> list[dict[str, Any]]:
    """Return the supported f32/f64 cases in the driver manifest format.

    ``ulp_limit`` is zero for exact rationally rounded operations, one for
    square root, and two for mpmath-backed libm operations. NaN results pass
    when both values are NaN regardless of payload/sign. Infinities and
    signed zeros require exact sign/value agreement; ULP tolerance applies
    only to finite nonzero results. The driver should retain raw C/Rust bit
    differences separately, especially for min/max NaN and signed-zero cases;
    semantic comparison must not discard that diagnostic.
    """
    result: list[dict[str, Any]] = []
    for bits in (32, 64):
        for name, op in _OPS:
            item = {
                "name": f"f{bits}_{name}",
                "kind": "float",
                "bits": bits,
                "op": op,
                "ulp_limit": 2 if name in _LIBM_OPS else (1 if name == "sqrt" else 0),
            }
            if name in ("min", "max"):
                item["zero_sign_required"] = False
            result.append(item)
    return result


def c_helpers() -> str:
    """The harness supplies aq_from_f32/f64 and aq_bits_f32/f64."""
    return ""


def r_helpers() -> str:
    """Rust's standard ``from_bits``/``to_bits`` methods need no helper."""
    return ""


def _precision(case: dict[str, Any]) -> tuple[int, int, int, int]:
    bits = int(case["bits"])
    if bits not in (32, 64):
        raise ValueError(f"unsupported float width: {bits}")
    return bits, (8 if bits == 32 else 11), (23 if bits == 32 else 52), (127 if bits == 32 else 1023)


def _c_float(bits: int, field: str) -> str:
    return f"aq_from_f{bits}(p->{field})"


def _c_bits(bits: int, expr: str) -> str:
    return f"aq_bits_f{bits}({expr})"


def c_body(case: dict[str, Any]) -> str:
    """Generate the body of one C kernel loop iteration."""
    bits = int(case["bits"])
    op = str(case["op"])
    fields = ("a", "b", "c")
    a, b, c = (_c_float(bits, f) for f in fields)
    suffix = "f" if bits == 32 else ""
    unary = {
        "recip": f"(1.0{suffix} / {a})",
        "sqrt": f"sqrt{suffix}({a})",
        "round": f"round{suffix}({a})",
        "round_ties_even": f"nearbyint{suffix}({a})",
        "floor": f"floor{suffix}({a})",
        "ceil": f"ceil{suffix}({a})",
        "trunc": f"trunc{suffix}({a})",
        "fract": f"({a} - trunc{suffix}({a}))",
        "abs": f"fabs{suffix}({a})",
        "neg": f"(-{a})",
    }
    if op in unary:
        expr = unary[op]
    elif op in ("+", "-", "*", "/", "%"):
        expr = f"({a} {op if op != '%' else '%'} {b})" if op != "/" else f"({a} / {b})"
        if op == "%":
            expr = f"fmod{suffix}({a}, {b})"
    elif op == "fma":
        expr = f"fma{suffix}({a}, {b}, {c})"
    elif op == "min":
        expr = f"fmin{suffix}({a}, {b})"
    elif op == "max":
        expr = f"fmax{suffix}({a}, {b})"
    elif op == "copysign":
        expr = f"copysign{suffix}({a}, {b})"
    elif op in ("powf", "hypot", "atan2"):
        cfn = {"powf": "pow", "hypot": "hypot", "atan2": "atan2"}[op]
        expr = f"{cfn}{suffix}({a}, {b})"
    elif op in ("exp", "exp2", "ln", "log2", "log10", "ln_1p", "exp_m1",
                "sin", "cos", "tan", "asin", "acos", "atan", "sinh",
                "cosh", "tanh", "asinh", "acosh", "atanh", "cbrt"):
        cfn = {"ln": "log", "ln_1p": "log1p", "exp_m1": "expm1"}.get(op, op)
        expr = f"{cfn}{suffix}({a})"
    else:
        raise ValueError(f"unsupported floating operation: {op}")
    return "\n".join((
        f"q->lo = {_c_bits(bits, expr)};",
        "q->hi = 0;",
        "q->flag = 0;",
        "q->reserved = 0;",
    ))


def r_body(case: dict[str, Any]) -> str:
    """Generate the body of one Rust kernel loop iteration."""
    bits = int(case["bits"])
    op = str(case["op"])
    ty = f"f{bits}"
    fields = ("a", "b", "c")
    a, b, c = (f"{ty}::from_bits(p.{field} as u{bits})" for field in fields)
    binary = {"+": f"{a} + {b}", "-": f"{a} - {b}", "*": f"{a} * {b}", "/": f"{a} / {b}", "%": f"{a} % {b}"}
    unary = {
        "recip": f"{a}.recip()",
        "sqrt": f"{a}.sqrt()",
        "round": f"{a}.round()",
        "round_ties_even": f"{a}.round_ties_even()",
        "floor": f"{a}.floor()",
        "ceil": f"{a}.ceil()",
        "trunc": f"{a}.trunc()",
        "fract": f"{a}.fract()",
        "abs": f"{a}.abs()",
        "neg": f"-{a}",
        "min": f"{a}.min({b})",
        "max": f"{a}.max({b})",
        "copysign": f"{a}.copysign({b})",
        "fma": f"{a}.mul_add({b}, {c})",
        "exp": f"{a}.exp()",
        "exp2": f"{a}.exp2()",
        "ln": f"{a}.ln()",
        "log2": f"{a}.log2()",
        "log10": f"{a}.log10()",
        "ln_1p": f"{a}.ln_1p()",
        "exp_m1": f"{a}.exp_m1()",
        "sin": f"{a}.sin()",
        "cos": f"{a}.cos()",
        "tan": f"{a}.tan()",
        "asin": f"{a}.asin()",
        "acos": f"{a}.acos()",
        "atan": f"{a}.atan()",
        "sinh": f"{a}.sinh()",
        "cosh": f"{a}.cosh()",
        "tanh": f"{a}.tanh()",
        "asinh": f"{a}.asinh()",
        "acosh": f"{a}.acosh()",
        "atanh": f"{a}.atanh()",
        "cbrt": f"{a}.cbrt()",
        "powf": f"{a}.powf({b})",
        "atan2": f"{a}.atan2({b})",
        "hypot": f"{a}.hypot({b})",
    }
    expr = binary[op] if op in binary else unary.get(op)
    if expr is None:
        raise ValueError(f"unsupported floating operation: {op}")
    return "\n".join((
        f"q.lo = ({expr}).to_bits() as u64;",
        "q.hi = 0;",
        "q.flag = 0;",
        "q.reserved = 0;",
    ))


def vector(case: dict[str, Any], index: int) -> tuple[int, int, int, int, int, int]:
    """Return a deterministic directed/random sample as six ABI bit fields.

    Each row contains the selected precision's bits in ``a/b/c`` and zeros in
    the upper-half ``ah/bh/ch`` fields. Directed samples cover signed zeros,
    infinities, NaNs, subnormals, and representative finite values. Random
    values stay away from extreme exponents, keeping the corpus reproducible
    and broadly useful across operations.
    """
    if index < 0:
        raise ValueError("sample index must be nonnegative")
    _precision(case)
    op = str(case.get("op", ""))
    f32_table = (
        (0x00000000, 0x00000000, 0x00000000),
        (0x80000000, 0x00000000, 0x80000000),
        (0x3F800000, 0x40000000, 0x40400000),
        (0xBF800000, 0x3F000000, 0x3F800000),
        (0x00000001, 0x007FFFFF, 0x00800000),
        (0x7F800000, 0xFF800000, 0x3F800000),
        (0x7FC12345, 0x3F800000, 0xFFC54321),
        (0x00800001, 0x80800001, 0x3EAAAAAB),
        (0x3F000000, 0xBF000000, 0x3F800000),
        (0x41200000, 0x3F800000, 0xC1200000),
        (0x3F800001, 0x3F7FFFFF, 0x3F000000),
        (0x7F7FFFFF, 0x00800000, 0x3F800000),
    )
    f64_table = (
        (0x0000000000000000, 0x0000000000000000, 0x0000000000000000),
        (0x8000000000000000, 0x0000000000000000, 0x8000000000000000),
        (0x3FF0000000000000, 0x4000000000000000, 0x4008000000000000),
        (0xBFF0000000000000, 0x3FE0000000000000, 0x3FF0000000000000),
        (0x0000000000000001, 0x000FFFFFFFFFFFFF, 0x0010000000000000),
        (0x7FF0000000000000, 0xFFF0000000000000, 0x3FF0000000000000),
        (0x7FF8123456789ABC, 0x3FF0000000000000, 0xFFF8ABCDEF012345),
        (0x0010000000000001, 0x8010000000000001, 0x3FD5555555555555),
        (0x3FE0000000000000, 0xBFE0000000000000, 0x3FF0000000000000),
        (0x4024000000000000, 0x3FF0000000000000, 0xC024000000000000),
        (0x3FF0000000000001, 0x3FEFFFFFFFFFFFFF, 0x3FE0000000000000),
        (0x7FEFFFFFFFFFFFFF, 0x0010000000000000, 0x3FF0000000000000),
    )
    if index < len(f32_table):
        values = f32_table[index] if int(case["bits"]) == 32 else f64_table[index]
    else:
        if op in _LIBM_OPS:
            values = _libm_vector(case, index)
        else:
            rng = random.Random(0xA51C_2026 + index)
            values = tuple(_random_finite(rng, int(case["bits"])) for _ in range(3))
    if op in _LIBM_OPS and index < len(f32_table):
        values = _libm_vector(case, index)
    return (*values, 0, 0, 0)


def _libm_vector(case: dict[str, Any], index: int) -> tuple[int, int, int]:
    """Mix bounded finite workloads with device-executed IEEE/domain edges."""
    op, bits = str(case["op"]), int(case["bits"])
    if 12 <= index < 26:
        one = _encode(Fraction(1), bits)
        zero, negative_zero = 0, 1 << (bits-1)
        infinity = _special("inf", 0, bits)
        negative_infinity = _special("inf", 1, bits)
        nan = _quiet_nan(bits)
        special = (
            (zero, zero), (negative_zero, one),
            (infinity, one), (negative_infinity, one), (nan, one),
            (1, one), (_encode(Fraction(-1), bits), _encode(Fraction(1,2), bits)),
            (one, nan), (zero, _encode(Fraction(-1), bits)),
            (negative_zero, _encode(Fraction(-3), bits)),
            (negative_zero, _encode(Fraction(3), bits)),
            (infinity, infinity), (negative_infinity, infinity), (one, negative_infinity),
        )
        a, b = special[index-12]
        return a, b, one
    table = {
        "powf": ((2, 3), (2, -2), (1, 0), (1, 3), (1, 0), (1, 2), (2, 1), (1, -1),
                 (2, 2), (1, 1), (3, 2), (4, 1)),
        "atan2": ((1, 1), (1, -1), (-1, 1), (-1, -1), (2, 1), (-2, 1), (1, 2),
                  (1, -2), (3, 4), (-3, 4), (4, 3), (4, -3)),
        "hypot": ((3, 4), (5, 12), (1, 1), (2, 3), (4, 3), (1, 2), (2, 2),
                  (3, 1), (1, 3), (4, 4), (5, 1), (1, 5)),
        "asin": ((-1, 0), (-1, 0), (-1, 0), (0, 0), (1, 0), (1, 0), (1, 0),
                 (0, 0), (1, 0), (-1, 0), (0, 0), (1, 0)),
        "acos": ((-1, 0), (-1, 0), (0, 0), (0, 0), (1, 0), (1, 0), (-1, 0),
                 (1, 0), (0, 0), (-1, 0), (1, 0), (0, 0)),
        "acosh": ((1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (2, 0), (3, 0),
                  (4, 0), (1, 0), (5, 0), (2, 0), (3, 0)),
        "atanh": ((-1, 0), (-1, 0), (0, 0), (0, 0), (1, 0), (1, 0), (-1, 0),
                  (1, 0), (0, 0), (-1, 0), (1, 0), (0, 0)),
        "ln": ((1, 0), (1, 0), (1, 0), (2, 0), (2, 0), (4, 0), (4, 0),
               (0, 0), (2, 0), (4, 0), (1, 0), (2, 0)),
        "log2": ((1, 0), (1, 0), (1, 0), (2, 0), (2, 0), (4, 0), (4, 0),
                 (0, 0), (2, 0), (4, 0), (1, 0), (2, 0)),
        "log10": ((1, 0), (1, 0), (1, 0), (2, 0), (2, 0), (4, 0), (4, 0),
                  (0, 0), (2, 0), (4, 0), (1, 0), (2, 0)),
        "ln_1p": ((-1, 0), (-1, 0), (0, 0), (0, 0), (1, 0), (1, 0), (-1, 0),
                  (1, 0), (0, 0), (-1, 0), (1, 0), (0, 0)),
    }
    if index < 12 and op in table:
        x, y = table[op][index]
        if op == "powf":
            a, b = Fraction(x), Fraction(y)
        elif op in ("atan2", "hypot"):
            a, b = Fraction(x), Fraction(y)
        elif op in ("asin", "acos", "atanh", "ln_1p"):
            a, b = Fraction(x, 1 if op in ("asin", "acos") else 2), Fraction(1)
        elif op == "acosh":
            a, b = Fraction(x), Fraction(1)
        elif op in ("ln", "log2", "log10"):
            a, b = Fraction(x, 4) if x else Fraction(1, 4), Fraction(1)
        else:
            a, b = Fraction(x, 2), Fraction(1)
    else:
        seed = 0xA51C_2026 + index * 7919 + sum(map(ord, op)) * 104729 + bits
        rng = random.Random(seed)
        a = Fraction(rng.randint(-8192, 8192), 2048)
        b = Fraction(rng.randint(-8192, 8192), 2048)
        c = Fraction(rng.randint(-8192, 8192), 2048)
        if op == "powf":
            a, b = abs(a) + Fraction(1, 8), b / 4
        elif op in ("ln", "log2", "log10"):
            a = abs(a) + Fraction(1, 16)
        elif op == "ln_1p":
            a /= 8
        elif op == "acosh":
            a = 1 + abs(a)
        elif op == "atanh":
            a /= 8
        elif op in ("asin", "acos"):
            a /= 4
        return _encode(a, bits), _encode(b, bits), _encode(c, bits)
    return _encode(a, bits), _encode(b, bits), _encode(Fraction(1), bits)


def expected(case: dict[str, Any], sample: tuple[int, int, int, int, int, int]) -> tuple[int, int, int, int]:
    """Compute result bits without using host floating arithmetic for finite values."""
    bits, _, _, _ = _precision(case)
    out = _oracle(str(case["op"]), tuple(int(x) for x in sample[:3]), bits)
    return out, 0, 0, 0


def _fmt(bits: int) -> tuple[int, int, int, int, int, int]:
    ebits = 8 if bits == 32 else 11
    fbits = 23 if bits == 32 else 52
    bias = 127 if bits == 32 else 1023
    return ebits, fbits, bias, (1 << (bits - 1)), (1 << fbits) - 1, (1 << ebits) - 1


def _classify(raw: int, bits: int) -> tuple[str, Fraction | None, int]:
    ebits, fbits, bias, signmask, fracmask, expmask = _fmt(bits)
    raw &= (1 << bits) - 1
    sign = 1 if raw & signmask else 0
    exponent = (raw >> fbits) & expmask
    fraction = raw & fracmask
    if exponent == expmask:
        return ("nan" if fraction else "inf", None, sign)
    if exponent == 0 and fraction == 0:
        return "finite", Fraction(0), sign
    if exponent == 0:
        value = Fraction(fraction, 1 << fbits) * _pow2(1 - bias)
    else:
        value = Fraction((1 << fbits) | fraction, 1 << fbits) * _pow2(exponent - bias)
    return "finite", -value if sign else value, sign


def classify(raw: int, bits: int) -> tuple[str, Fraction | None, int]:
    """Decode IEEE f32/f64 bits as ``(kind, exact value, sign bit)``.

    Finite values are exact ``Fraction`` instances. Zero is represented by a
    zero fraction plus its independent sign bit; ``kind`` is ``finite``,
    ``inf``, or ``nan``.
    """
    if bits not in (32, 64):
        raise ValueError(f"unsupported float width: {bits}")
    return _classify(raw, bits)


def _pow2(exponent: int) -> Fraction:
    return Fraction(1 << exponent, 1) if exponent >= 0 else Fraction(1, 1 << -exponent)


def _round_ratio_even(numerator: int, denominator: int) -> int:
    q, r = divmod(numerator, denominator)
    twice = r * 2
    return q + (twice > denominator or (twice == denominator and q & 1))


def _floor_log2(value: Fraction) -> int:
    n, d = value.numerator, value.denominator
    e = n.bit_length() - d.bit_length()
    if (value < _pow2(e)):
        e -= 1
    return e


def _encode(value: Fraction, bits: int, zero_sign: int = 0) -> int:
    ebits, fbits, bias, signmask, _, expmask = _fmt(bits)
    sign = int(value < 0) if value else int(bool(zero_sign))
    magnitude = abs(value)
    if not magnitude:
        return sign * signmask
    emin = 1 - bias
    emax = expmask - 1 - bias
    exponent = _floor_log2(magnitude)
    if exponent < emin:
        scaled = magnitude / _pow2(emin - fbits)
        significand = _round_ratio_even(scaled.numerator, scaled.denominator)
        if significand == 0:
            return sign * signmask
        if significand >= (1 << fbits):
            return (sign * signmask) | (1 << fbits)
        return (sign * signmask) | significand
    scaled = magnitude / _pow2(exponent - fbits)
    significand = _round_ratio_even(scaled.numerator, scaled.denominator)
    if significand == (1 << (fbits + 1)):
        significand >>= 1
        exponent += 1
    if exponent > emax:
        return (sign * signmask) | (expmask << fbits)
    return (sign * signmask) | ((exponent + bias) << fbits) | (significand - (1 << fbits))


def encode_fraction(value: Fraction, bits: int, zero_sign: int = 0) -> int:
    """Round an exact rational to IEEE binary32/binary64, ties to even.

    ``zero_sign`` selects the sign bit when ``value`` is exactly zero.
    Overflow rounds to signed infinity and underflow preserves the input sign.
    """
    if bits not in (32, 64):
        raise ValueError(f"unsupported float width: {bits}")
    if not isinstance(value, Fraction):
        raise TypeError("value must be fractions.Fraction")
    return _encode(value, bits, zero_sign)


def _special(kind: str, sign: int, bits: int) -> int:
    ebits, fbits, _, signmask, _, expmask = _fmt(bits)
    raw = (sign * signmask) | (expmask << fbits)
    return raw | ((1 << (fbits - 1)) if kind == "nan" else 0)


def _quiet_nan(bits: int) -> int:
    return _special("nan", 0, bits)


def _trunc_fraction(value: Fraction) -> int:
    return abs(value.numerator) // value.denominator * (-1 if value < 0 else 1)


def _oracle(op: str, raw: tuple[int, ...], bits: int) -> int:
    decoded = tuple(_classify(x, bits) for x in raw)
    kinds = tuple(x[0] for x in decoded)
    vals = tuple(x[1] for x in decoded)
    signs = tuple(x[2] for x in decoded)
    nan = _quiet_nan(bits)

    def fin(value: Fraction, sign: int = 0) -> int:
        return _encode(value, bits, sign)

    def infinity(sign: int) -> int:
        return _special("inf", sign, bits)

    if op in _LIBM_OPS:
        return _oracle_libm(op, raw, decoded, bits)

    if op in ("+", "-"):
        sign_b = signs[1] ^ int(op == "-")
        if "nan" in kinds[:2]:
            return nan
        if kinds[0] == "inf" or kinds[1] == "inf":
            if kinds[0] == kinds[1] == "inf" and signs[0] != sign_b:
                return nan
            return infinity(signs[0] if kinds[0] == "inf" else sign_b)
        assert vals[0] is not None and vals[1] is not None
        value = vals[0] + (vals[1] if op == "+" else -vals[1])
        zsign = (signs[0] if vals[0] == 0 else sign_b) if value == 0 and vals[0] == vals[1] == 0 and signs[0] == sign_b else 0
        return fin(value, zsign)
    if op in ("*", "/", "recip"):
        if op == "recip":
            left, right = ("finite", Fraction(1), 0), decoded[0]
            lk, lv, ls = left
            rk, rv, rs = right
        else:
            lk, lv, ls = decoded[0]
            rk, rv, rs = decoded[1]
        if lk == "nan" or rk == "nan":
            return nan
        sign = ls ^ rs
        if op == "*":
            if (lk == "inf" and rk == "finite" and rv == 0) or (rk == "inf" and lk == "finite" and lv == 0):
                return nan
            if lk == "inf" or rk == "inf":
                return infinity(sign)
            assert lv is not None and rv is not None
            return fin(lv * rv, sign)
        if lk == rk == "inf" or (lk == "finite" and lv == 0 and rk == "finite" and rv == 0):
            return nan
        if lk == "inf" or (rk == "finite" and rv == 0):
            return infinity(sign)
        if rk == "inf":
            return fin(Fraction(0), sign)
        assert lv is not None and rv is not None
        return fin(lv / rv, sign)
    if op == "fma":
        if "nan" in kinds[:3]:
            return nan
        if (kinds[0] == "inf" and kinds[1] == "finite" and vals[1] == 0) or (kinds[1] == "inf" and kinds[0] == "finite" and vals[0] == 0):
            return nan
        product_sign = signs[0] ^ signs[1]
        if kinds[0] == "inf" or kinds[1] == "inf":
            if kinds[2] == "inf" and signs[2] != product_sign:
                return nan
            return infinity(product_sign)
        if kinds[2] == "inf":
            return infinity(signs[2])
        assert vals[0] is not None and vals[1] is not None and vals[2] is not None
        product = vals[0] * vals[1]
        return fin(product + vals[2], product_sign if product == vals[2] == 0 and product_sign == signs[2] else 0)
    if op == "%":
        if "nan" in kinds[:2] or kinds[0] == "inf" or (kinds[1] == "finite" and vals[1] == 0):
            return nan
        if kinds[1] == "inf":
            return raw[0]
        assert vals[0] is not None and vals[1] is not None
        quotient = _trunc_fraction(vals[0] / vals[1])
        return fin(vals[0] - quotient * vals[1], signs[0])
    if op == "sqrt":
        kind, value, sign = decoded[0]
        if kind == "nan":
            return nan
        if kind == "inf":
            return nan if sign else infinity(0)
        assert value is not None
        if value < 0:
            return nan
        if value == 0:
            return fin(value, sign)
        return _sqrt_round(value, bits)
    if op in ("round", "round_ties_even", "floor", "ceil", "trunc", "fract"):
        kind, value, sign = decoded[0]
        if kind != "finite":
            return nan if kind == "nan" or op == "fract" else raw[0]
        assert value is not None
        if op == "fract":
            return fin(value - _trunc_fraction(value), 0)
        n, d = value.numerator, value.denominator
        if op == "floor":
            rounded = n // d
        elif op == "ceil":
            rounded = -((-n) // d)
        elif op == "trunc":
            rounded = _trunc_fraction(value)
        elif op == "round_ties_even":
            rounded = _round_ratio_even(abs(n), d) * (-1 if n < 0 else 1)
        else:
            rounded = ((2 * abs(n) + d) // (2 * d)) * (-1 if n < 0 else 1)
        zsign = sign if rounded == 0 else 0
        return fin(Fraction(rounded), zsign)
    if op in ("abs", "neg"):
        kind, value, sign = decoded[0]
        if kind == "nan":
            return nan
        if kind == "inf":
            return infinity(0 if op == "abs" else sign ^ 1)
        assert value is not None
        return fin(abs(value) if op == "abs" else -value, 0 if op == "abs" else sign ^ 1)
    if op in ("min", "max"):
        if kinds[0] == kinds[1] == "nan":
            return nan
        if kinds[0] == "nan":
            return raw[1]
        if kinds[1] == "nan":
            return raw[0]
        if kinds[0] == "inf":
            left_cmp = -1 if signs[0] else 1
        else:
            left_cmp = 0
        if kinds[1] == "inf":
            right_cmp = -1 if signs[1] else 1
        else:
            right_cmp = 0
        if kinds[0] == "inf" and kinds[1] == "inf":
            cmp = (left_cmp > right_cmp) - (left_cmp < right_cmp)
        elif kinds[0] == "inf":
            cmp = left_cmp
        elif kinds[1] == "inf":
            cmp = -right_cmp
        else:
            assert vals[0] is not None and vals[1] is not None
            cmp = (vals[0] > vals[1]) - (vals[0] < vals[1])
        if cmp == 0 and vals[0] == vals[1] == 0:
            return fin(Fraction(0), 1 if op == "min" and (signs[0] or signs[1]) else 0)
        chosen = 0 if ((op == "min" and cmp <= 0) or (op == "max" and cmp >= 0)) else 1
        return raw[chosen]
    if op == "copysign":
        if kinds[0] == "nan":
            return nan
        signmask = _fmt(bits)[3]
        return (raw[0] & ~signmask) | (raw[1] & signmask)
    raise ValueError(f"unsupported oracle operation: {op}")


def _mp_input(kind: str, value: Fraction | None, sign: int) -> Any:
    if kind == "nan":
        return mpmath.mpf("nan")
    if kind == "inf":
        return mpmath.mpf("-inf" if sign else "inf")
    assert value is not None
    if value == 0:
        return mpmath.mpf("-0.0" if sign else "0.0")
    return mpmath.mpf(value.numerator) / value.denominator


def _mp_result(value: Any, bits: int, zero_sign: int = 0) -> int:
    if isinstance(value, mpmath.mpc):
        return _quiet_nan(bits) if value.imag else _mp_result(value.real, bits, zero_sign)
    if mpmath.isnan(value):
        return _quiet_nan(bits)
    if mpmath.isinf(value):
        return _special("inf", int(value < 0), bits)
    sign, man, exponent, _ = value._mpf_
    _, fbits, bias, signmask, _, expmask = _fmt(bits)
    if man == 0:
        return _encode(Fraction(0), bits, 1 if sign else zero_sign)
    top_exponent = exponent + man.bit_length() - 1
    emax = expmask - 1 - bias
    emin = 1 - bias
    if top_exponent > emax:
        return _special("inf", sign, bits)
    if top_exponent < emin - fbits - 1:
        return sign * signmask
    exact = Fraction(man << exponent, 1) if exponent >= 0 else Fraction(man, 1 << -exponent)
    if sign:
        exact = -exact
    return _encode(exact, bits)


def _oracle_libm(
    op: str,
    raw: tuple[int, ...],
    decoded: tuple[tuple[str, Fraction | None, int], ...],
    bits: int,
) -> int:
    binary = op in ("powf", "atan2", "hypot")
    xkind, xvalue, xsign = decoded[0]
    if op == "hypot" and (xkind == "inf" or decoded[1][0] == "inf"):
        return _special("inf", 0, bits)
    if op == "powf":
        ykind, yvalue, ysign = decoded[1]
        # IEEE pow identities take precedence over NaN propagation.
        if (ykind == "finite" and yvalue == 0) or (xkind == "finite" and xvalue == 1):
            return _encode(Fraction(1), bits)
        if xkind == "nan" or ykind == "nan":
            return _quiet_nan(bits)
        if xkind == "inf":
            if ykind == "inf":
                return _encode(Fraction(0), bits) if ysign else _special("inf", 0, bits)
            assert yvalue is not None
            odd = bool(xsign and yvalue.denominator == 1 and yvalue.numerator & 1)
            return (_encode(Fraction(0), bits, int(odd)) if yvalue < 0
                    else _special("inf", int(odd), bits))
        if xkind == "finite" and xvalue == 0:
            if ykind == "inf":
                return _special("inf", 0, bits) if ysign else _encode(Fraction(0), bits)
            assert yvalue is not None
            odd_negative = bool(xsign and yvalue.denominator == 1 and yvalue.numerator & 1)
            if yvalue < 0:
                return _special("inf", int(odd_negative), bits)
            return _encode(Fraction(0), bits, int(odd_negative))
    if xkind == "nan":
        return _quiet_nan(bits)
    if binary and decoded[1][0] == "nan":
        return _quiet_nan(bits)
    unary = {
        "exp": mpmath.exp, "exp2": lambda a: mpmath.power(2, a),
        "ln": mpmath.log, "log2": lambda a: mpmath.log(a, 2),
        "log10": lambda a: mpmath.log(a, 10),
        "ln_1p": lambda a: mpmath.log1p(a),
        "exp_m1": lambda a: mpmath.expm1(a),
        "sin": mpmath.sin, "cos": mpmath.cos, "tan": mpmath.tan,
        "asin": mpmath.asin, "acos": mpmath.acos, "atan": mpmath.atan,
        "sinh": mpmath.sinh, "cosh": mpmath.cosh, "tanh": mpmath.tanh,
        "asinh": mpmath.asinh, "acosh": mpmath.acosh, "atanh": mpmath.atanh,
    }
    try:
        with mpmath.workdps(100):
            x, y = tuple(_mp_input(*item) for item in decoded[:2])
            if op == "atan2":
                special_angle = _atan2_special(decoded[0], decoded[1], bits)
                if special_angle is not None:
                    return special_angle
            if op == "atanh" and xkind == "finite" and xvalue in (Fraction(1), Fraction(-1)):
                return _special("inf", int(xvalue < 0), bits)
            if xkind == "inf" and op in ("sinh", "cosh", "tanh", "asinh", "acosh"):
                if op in ("sinh", "asinh"):
                    return _special("inf", xsign, bits)
                if op == "cosh":
                    return _special("inf", 0, bits)
                if op == "tanh":
                    return _encode(Fraction(-1 if xsign else 1), bits)
                if op == "acosh" and not xsign:
                    return _special("inf", 0, bits)
                if op == "acosh":
                    return _quiet_nan(bits)
            # mpmath's mpf canonicalizes negative zero to positive zero.
            # Preserve IEEE sign for odd functions that map zero to zero.
            if decoded[0][0] == "finite" and decoded[0][1] == 0 and decoded[0][2]:
                if op in ("sin", "tan", "asin", "atan", "sinh", "tanh", "asinh", "ln_1p", "exp_m1", "atanh", "cbrt"):
                    return _encode(Fraction(0), bits, 1)
            if op in unary:
                result = unary[op](x)
            elif op == "powf":
                result = mpmath.power(x, y)
            elif op == "atan2":
                result = mpmath.atan2(x, y)
            elif op == "hypot":
                result = mpmath.hypot(x, y)
            elif op == "cbrt":
                result = mpmath.sign(x) * mpmath.power(abs(x), mpmath.mpf(1) / 3)
            else:
                raise ValueError(f"unsupported libm operation: {op}")
            return _mp_result(result, bits)
    except (ValueError, ZeroDivisionError, OverflowError):
        return _quiet_nan(bits)


def _atan2_special(
    y: tuple[str, Fraction | None, int],
    x: tuple[str, Fraction | None, int],
    bits: int,
) -> int | None:
    ykind, yvalue, ysign = y
    xkind, xvalue, xsign = x
    yzero = ykind == "finite" and yvalue == 0
    xzero = xkind == "finite" and xvalue == 0
    yinf = ykind == "inf"
    xinf = xkind == "inf"
    if not (yzero or xzero or yinf or xinf):
        return None
    with mpmath.workdps(100):
        pi = mpmath.pi
        if yzero and xzero:
            if not xsign:
                return _encode(Fraction(0), bits, ysign)
            angle = -pi if ysign else pi
        elif yinf and xinf:
            magnitude = 3 * pi / 4 if xsign else pi / 4
            angle = -magnitude if ysign else magnitude
        elif yzero:
            if not xsign:
                return _encode(Fraction(0), bits, ysign)
            angle = -pi if ysign else pi
        elif xzero or yinf:
            angle = -pi / 2 if ysign else pi / 2
        elif xinf:
            if not xsign:
                return _encode(Fraction(0), bits, ysign)
            angle = -pi if ysign else pi
        else:
            return None
        return _mp_result(angle, bits)


def _sqrt_round(value: Fraction, bits: int) -> int:
    """Correctly round sqrt(value) by comparing exact rational midpoints."""
    if value == 0:
        return 0
    ebits, fbits, _, signmask, _, expmask = _fmt(bits)
    max_finite = ((expmask - 1) << fbits) | ((1 << fbits) - 1)
    lo, hi = 0, max_finite
    while lo < hi:
        mid = (lo + hi + 1) // 2
        kind, candidate, _ = _classify(mid, bits)
        assert kind == "finite" and candidate is not None
        if candidate * candidate <= value:
            lo = mid
        else:
            hi = mid - 1
    low = lo
    _, low_value, _ = _classify(low, bits)
    assert low_value is not None
    if low_value * low_value == value or low == max_finite:
        return low
    high = low + 1
    _, high_value, _ = _classify(high, bits)
    assert high_value is not None
    midpoint = (low_value + high_value) / 2
    midpoint_sq = midpoint * midpoint
    if value < midpoint_sq:
        return low
    if value > midpoint_sq:
        return high
    return low if (low & 1) == 0 else high


def _random_finite(rng: random.Random, bits: int) -> int:
    _, fbits, _, signmask, fracmask, expmask = _fmt(bits)
    emax = expmask - 1
    bias = 127 if bits == 32 else 1023
    # Keep random values in a moderate range; directed rows cover the ends.
    exponent = rng.randint(max(1, bias - 20), min(emax, bias + 20))
    return (rng.getrandbits(1) * signmask) | (exponent << fbits) | (rng.getrandbits(fbits) & fracmask)
