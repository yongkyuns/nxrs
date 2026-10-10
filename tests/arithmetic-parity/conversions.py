"""Conversions with explicit Rust-compatible C range guards.

Out-of-range float-to-integer casts are undefined in C, but saturate in Rust.
The C comparator implements that same contract rather than invoking UB.
"""
from fractions import Fraction
import floating
import integers


def operations():
    cases = []
    for name, bits, signed in integers.TYPES:
        for fb in (32, 64):
            cases += [dict(name=f"{name}_as_f{fb}", kind="conversion", op="int_to_float",
                           bits=fb, source_bits=bits, signed=signed, type=name,
                           float_bits=fb, ulp_limit=0, c_available=bits<=64),
                      dict(name=f"f{fb}_as_{name}", kind="conversion", op="float_to_int",
                           bits=bits, source_bits=fb, signed=signed, type=name,
                           float_bits=0, ulp_limit=0, c_available=bits<=64)]
    for src, dst in ((32, 64), (64, 32)):
        cases.append(dict(name=f"f{src}_as_f{dst}", kind="conversion", op="float_to_float",
                          bits=dst, source_bits=src, float_bits=dst, ulp_limit=0, c_available=True))
    for bits in (32, 64):
        cases.append(dict(name=f"f{bits}_powi", kind="conversion", op="powi", bits=bits,
                          source_bits=bits, float_bits=bits, ulp_limit=4, c_available=True))
    return cases


def vector(case, index):
    if case["op"] == "int_to_float":
        return integers.vector({"bits":case["source_bits"], "op":"cast", "mode":"plain"}, index)
    value = floating.vector({"bits":case["source_bits"]}, index)
    if case["op"] == "powi":
        return (value[0], (index%17-8)&0xffffffff, 0, 0, 0, 0)
    return value


def expected(case, vector):
    op, bits, source = case["op"], case["bits"], case["source_bits"]
    if op == "int_to_float":
        value = (vector[0] | vector[3]<<64) & ((1 << source)-1)
        if case["signed"]: value = integers.signed_value(value, source)
        return floating.encode_fraction(Fraction(value), bits), 0, 0, 0
    if op == "powi":
        exponent = integers.signed_value(vector[1], 32)
        encoded = floating.encode_fraction(Fraction(exponent), bits)
        return floating.expected({"bits":bits, "op":"powf"}, (vector[0],encoded,0,0,0,0))
    kind, value, sign = floating.classify(vector[0], source)
    if op == "float_to_float":
        if kind == "nan": result = 0x7fc00000 if bits==32 else 0x7ff8000000000000
        elif kind == "inf": result = (0x7f800000 if bits==32 else 0x7ff0000000000000) | sign<<(bits-1)
        else: result = floating.encode_fraction(value,bits,sign)
        return result,0,0,0
    lower, upper = (-(1<<(bits-1)), (1<<(bits-1))-1) if case["signed"] else (0,(1<<bits)-1)
    if kind=="nan": result=0
    elif kind=="inf": result=lower if sign else upper
    else:
        result = abs(value.numerator)//value.denominator * (-1 if value<0 else 1)
        result = min(upper,max(lower,result))
    result &= (1<<bits)-1
    return result&((1<<64)-1),result>>64,0,0


def r_body(case):
    op, bits, source = case["op"], case["bits"], case["source_bits"]
    if op=="int_to_float":
        typ={"usize":"AqUsize","isize":"AqIsize"}.get(case["type"],case["type"])
        value=f"((((p.a as u128)|((p.ah as u128)<<64)) as {typ}) as f{bits}).to_bits() as u128"
    elif op=="float_to_float":
        value=f"(f{source}::from_bits(p.a as u{source}) as f{bits}).to_bits() as u128"
    elif op=="powi":
        value=f"f{bits}::from_bits(p.a as u{bits}).powi(p.b as i32).to_bits() as u128"
    else:
        typ={"usize":"AqUsize","isize":"AqIsize"}.get(case["type"],case["type"])
        value=f"(f{source}::from_bits(p.a as u{source}) as {typ}) as u128"
        if bits<128: value=f"({value}) & {(1<<bits)-1}u128"
    return f"let value={value};q.lo=value as u64;q.hi=(value>>64) as u64;q.flag=0;q.reserved=0;"


def c_body(case):
    if not case["c_available"]: raise ValueError("no native C equivalent")
    op, bits, source = case["op"], case["bits"], case["source_bits"]
    if op=="int_to_float":
        typ=f"{'int' if case['signed'] else 'uint'}{source}_t"
        cast="float" if bits==32 else "double"
        expression=f"aq_bits_f{bits}(({cast})({typ})p->a)"
    elif op=="float_to_float":
        cast="float" if bits==32 else "double"
        expression=f"aq_bits_f{bits}(({cast})aq_from_f{source}(p->a))"
    elif op=="powi":
        typ="float" if bits==32 else "double"
        return f"{typ} base=aq_from_f{bits}(p->a), value=1;int32_t power=(int32_t)p->b;uint32_t n=power<0 ? 0u-(uint32_t)power:(uint32_t)power;while(n){{if(n&1)value*=base;n>>=1;if(n)base*=base;}}if(power<0)value=1/value;q->lo=aq_bits_f{bits}(value);q->hi=0;q->flag=0;q->reserved=0;"
    else:
        typ=f"{'int' if case['signed'] else 'uint'}{bits}_t"
        upper=f"INT{bits}_MAX" if case["signed"] else f"UINT{bits}_MAX"
        lower=f"INT{bits}_MIN" if case["signed"] else "0"
        lowerfloat=f"-0x1p{bits-1}" if case["signed"] else "0"
        upperfloat=f"0x1p{bits-1 if case['signed'] else bits}"
        return f"double value=(double)aq_from_f{source}(p->a);{typ} v;if(value!=value)v=0;else if(value<={lowerfloat})v={lower};else if(value>={upperfloat})v={upper};else v=({typ})value;q->lo=(uint64_t)(uint{bits}_t)v;q->hi=0;q->flag=0;q->reserved=0;"
    return f"q->lo={expression};q->hi=0;q->flag=0;q->reserved=0;"
