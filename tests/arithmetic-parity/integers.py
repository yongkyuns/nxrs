"""Integer catalog and independent arbitrary-precision expectations.

C uses defined unsigned arithmetic and explicit overflow guards. Rust keeps
its ordinary primitive methods. Native C has no 128-bit type on this target;
those entries are Rust-only correctness tests, not fabricated speed ratios.
"""
import random
import math

TYPES = [(f"{prefix}{bits}", bits, prefix == "i")
         for bits in (8, 16, 32, 64, 128) for prefix in ("u", "i")]
TYPES += [("usize", 32, False), ("isize", 32, True)]
MODES = ("wrapping", "checked", "saturating", "overflowing")


def operations():
    cases = []
    for name, bits, signed in TYPES:
        for op in ("add", "sub", "mul", "div", "rem", "pow", "neg"):
            for mode in MODES:
                # There is no saturating remainder in the primitive API.
                if mode == "saturating" and (op == "rem" or (op == "neg" and not signed)):
                    continue
                cases.append(dict(name=f"{name}_{mode}_{op}", kind="integer",
                                  type=name, bits=bits, signed=signed, op=op, mode=mode))
        for op in ("shl", "shr"):
            for mode in ("wrapping", "checked", "overflowing"):
                cases.append(dict(name=f"{name}_{mode}_{op}", kind="integer",
                                  type=name, bits=bits, signed=signed, op=op, mode=mode))
        simple = ["copy", "and", "or", "xor", "not", "rotate_left", "rotate_right",
                  "rotate_left_const", "rotate_right_const", "count_ones",
                  "count_zeros", "leading_zeros", "trailing_zeros", "leading_ones",
                  "trailing_ones", "swap_bytes", "reverse_bits", "min", "max", "cmp",
                  "eq", "ne", "lt", "le", "gt", "ge", "abs_diff", "checked_ilog",
                  "checked_ilog2", "checked_ilog10"]
        simple += ["checked_isqrt", "signum"] if signed else ["isqrt", "checked_next_power_of_two",
                                                               "div_ceil", "checked_next_multiple_of", "is_multiple_of"]
        for op in simple:
            cases.append(dict(name=f"{name}_{op}", kind="integer", type=name,
                              bits=bits, signed=signed, op=op, mode="plain"))
        if signed:
            for op in ("abs", "div_euclid", "rem_euclid"):
                for mode in MODES:
                    if op.endswith("euclid") and mode == "saturating":
                        continue
                    cases.append(dict(name=f"{name}_{mode}_{op}", kind="integer",
                                      type=name, bits=bits, signed=True, op=op, mode=mode))
    for src, bits, signed in TYPES:
        for dst, dbits, dsigned in TYPES:
            cases.append(dict(name=f"{src}_as_{dst}", kind="integer", type=src,
                              bits=bits, signed=signed, op="cast", mode="plain",
                              destination=dst, destination_bits=dbits,
                              destination_signed=dsigned))
    for bits in (32, 64):
        cases.append(dict(name=f"u{bits}_mixed_recurrence", kind="integer", type=f"u{bits}",
                          bits=bits, signed=False, op="mixed", mode="wrapping"))
        cases.append(dict(name=f"u{bits}_loop_boundaries", kind="integer", type=f"u{bits}",
                          bits=bits, signed=False, op="loop", mode="wrapping"))
    for case in cases:
        case.update(ulp_limit=0, c_available=case["bits"] <= 64 and case.get("destination_bits", 0) <= 64)
    return cases


def signed_value(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def vector(case, index):
    bits = case["bits"]
    mask = (1 << bits) - 1
    edges = [0, 1, 2, 3, mask, mask - 1, 1 << (bits - 1),
             (1 << (bits - 1)) - 1, (1 << (bits - 1)) + 1,
             int("55" * (bits // 8), 16), int("aa" * (bits // 8), 16), bits - 1, bits, bits + 1]
    rng = random.Random(0xA017_2026 + bits * 1009 + index)
    a = edges[index % len(edges)] if index < 32 else rng.getrandbits(bits)
    b = edges[(index * 5 + index // len(edges)) % len(edges)] if index < 32 else rng.getrandbits(bits)
    boundary_pairs = [(0, 0), (0, 1), (1, 0), (1, 1), (1 << (bits - 1), mask),
                      ((1 << (bits - 1)) - 1, 1), (mask, 1), (mask, mask),
                      (1 << (bits - 1), 0), (1 << (bits - 1), 1)]
    if index < len(boundary_pairs):
        a, b = boundary_pairs[index]
    c = rng.getrandbits(bits)
    if case["op"] == "pow":
        b = index % 9
    if case["op"] in ("shl", "shr", "rotate_left", "rotate_right"):
        b = [0, 1, bits - 1, bits, bits + 1, 2 * bits - 1, 0xffffffff][index % 7]
    if case["op"] in ("div", "rem", "div_euclid", "rem_euclid") and case["mode"] != "checked" and not b:
        b = 1
    if case["op"] == "loop":
        b = [0, 1, 2, 3, 7, 255, 256, 257, 1024][index % 9]
    if case["op"] == "div_ceil" and not b:
        b = 1
    low = (1 << 64) - 1
    return (a & low, b & low, c & low, a >> 64, b >> 64, c >> 64)


def expected(case, inputs):
    bits, signed, op, mode = (case[k] for k in ("bits", "signed", "op", "mode"))
    mask = (1 << bits) - 1
    a = (inputs[0] | inputs[3] << 64) & mask
    b = (inputs[1] | inputs[4] << 64) & mask
    c = (inputs[2] | inputs[5] << 64) & mask
    x, y = (signed_value(a, bits), signed_value(b, bits)) if signed else (a, b)
    lower, upper = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, mask)
    overflow = False
    if op == "copy":
        value = a
    elif op in ("add", "sub", "mul", "pow", "neg", "abs"):
        value = {"add": lambda: x + y, "sub": lambda: x - y, "mul": lambda: x * y,
                 "pow": lambda: x ** b, "neg": lambda: -x, "abs": lambda: abs(x)}[op]()
        overflow = not lower <= value <= upper
    elif op in ("div", "rem", "div_euclid", "rem_euclid"):
        overflow = not y or (signed and x == lower and y == -1)
        if not y:
            value = 0
        else:
            quotient = abs(x) // abs(y) * (-1 if (x < 0) != (y < 0) else 1)
            remainder = x - quotient * y
            if op.endswith("euclid") and remainder < 0:
                remainder += abs(y)
                quotient -= 1 if y > 0 else -1
            value = quotient if op.startswith("div") else remainder
    elif op in ("shl", "shr"):
        overflow = b >= bits
        count = b % bits
        value = a << count if op == "shl" else x >> count
    elif op in ("and", "or", "xor", "not"):
        value = {"and": lambda: a & b, "or": lambda: a | b,
                 "xor": lambda: a ^ b, "not": lambda: ~a}[op]()
    elif op.startswith("rotate_"):
        count = (5 if op.endswith("_const") else b) % bits
        value = ((a << count) | (a >> ((bits - count) % bits))) if op.startswith("rotate_left") else ((a >> count) | (a << ((bits - count) % bits)))
    elif op in ("count_ones", "count_zeros", "leading_zeros", "trailing_zeros", "leading_ones", "trailing_ones"):
        v = (~a & mask) if op.endswith("ones") and op != "count_ones" else a
        if op == "count_ones": value = a.bit_count()
        elif op == "count_zeros": value = bits - a.bit_count()
        elif op.startswith("leading_"): value = bits - v.bit_length()
        else: value = bits if not v else (v & -v).bit_length() - 1
    elif op == "swap_bytes":
        value = int.from_bytes(a.to_bytes(bits // 8, "little"), "big")
    elif op == "reverse_bits":
        value = int(f"{a:0{bits}b}"[::-1], 2)
    elif op in ("min", "max", "cmp"):
        value = min(x, y) if op == "min" else max(x, y) if op == "max" else (x > y) - (x < y)
        if op == "cmp": bits, mask = 64, (1 << 64) - 1
    elif op in ("eq", "ne", "lt", "le", "gt", "ge"):
        value = int({"eq": x == y, "ne": x != y, "lt": x < y,
                     "le": x <= y, "gt": x > y, "ge": x >= y}[op])
    elif op == "abs_diff":
        value = abs(x-y)
    elif op in ("isqrt", "checked_isqrt"):
        overflow = x < 0
        value = 0 if overflow else math.isqrt(x)
        mode = "checked" if op.startswith("checked_") else mode
    elif op.startswith("checked_ilog"):
        base = y if op == "checked_ilog" else 2 if op == "checked_ilog2" else 10
        overflow = x <= 0 or base <= 1
        value, v = 0, x
        if not overflow:
            while v >= base: value, v = value + 1, v // base
        mode = "checked"
    elif op == "checked_next_power_of_two":
        value = 1 << ((a-1).bit_length() if a else 0)
        overflow, mode = value > mask, "checked"
    elif op in ("div_ceil", "checked_next_multiple_of", "is_multiple_of"):
        if op == "is_multiple_of": value = int(not a if not b else a % b == 0)
        else:
            value = 0 if not b else (a+b-1)//b
            if op == "checked_next_multiple_of":
                value *= b
                overflow, mode = not b or value > mask, "checked"
    elif op == "signum":
        value = (x>0)-(x<0)
    elif op == "cast":
        bits, mask = case["destination_bits"], (1 << case["destination_bits"]) - 1
        value = x
    elif op == "mixed":
        value = a
        for i in range(64):
            value = (((value << 5) | (value >> (bits - 5))) * 0x9e3779b9 + (c ^ (i * 0x7f4a7c15 & mask))) & mask
    elif op == "loop":
        value = (a + b * (b - 1) // 2) & mask
    else:
        raise ValueError(f"unknown integer operation: {op}")
    if mode == "checked" and overflow:
        value = 0
    elif mode == "saturating":
        value = min(upper, max(lower, value))
    value &= mask
    return value & ((1 << 64) - 1), value >> 64, int(overflow and mode in ("checked", "overflowing")), 0


def r_body(case):
    typ, op, mode = (case[k] for k in ("type", "op", "mode"))
    typ = {"usize": "AqUsize", "isize": "AqIsize"}.get(typ, typ)
    high = " | ((p.ah as u128) << 64)" if case["bits"] == 128 else ""
    highb = " | ((p.bh as u128) << 64)" if case["bits"] == 128 else ""
    a = f"((p.a as u128){high}) as {typ}"
    b = f"((p.b as u128){highb}) as {typ}"
    start = f"let a={a}; let b={b}; let mut flag=0u32;\n"
    if op == "copy":
        expression = "let value=a;"
    elif op in ("add", "sub", "mul", "div", "rem", "pow", "neg", "abs", "div_euclid", "rem_euclid", "shl", "shr"):
        arg = "" if op in ("neg", "abs") else "p.b as u32" if op in ("pow", "shl", "shr") else "b"
        call = f"a.{mode}_{op}({arg})"
        if mode == "checked": expression = f"let v={call}; flag=u32::from(v.is_none()); let value=v.unwrap_or(0);"
        elif mode == "overflowing": expression = f"let (value,overflow)={call}; flag=u32::from(overflow);"
        else: expression = f"let value={call};"
    elif op in ("and", "or", "xor", "not"):
        expression = f"let value={ '!a' if op == 'not' else 'a '+{'and':'&','or':'|','xor':'^'}[op]+' b' };"
    elif op.startswith("rotate_"):
        method = op.removesuffix("_const")
        expression = f"let value=a.{method}({'5' if op.endswith('_const') else 'p.b as u32'});"
    elif op in ("count_ones", "count_zeros", "leading_zeros", "trailing_zeros", "leading_ones", "trailing_ones", "swap_bytes", "reverse_bits"):
        expression = f"let value=a.{op}();"
    elif op in ("min", "max"):
        expression = f"let value=a.{op}(b);"
    elif op in ("eq", "ne", "lt", "le", "gt", "ge"):
        expression = f"let value=(a { {'eq':'==','ne':'!=','lt':'<','le':'<=','gt':'>','ge':'>='}[op]} b) as u32;"
    elif op in ("abs_diff", "div_ceil", "is_multiple_of"):
        expression = f"let value=a.{op}(b);"
    elif op in ("isqrt", "signum"):
        expression = f"let value=a.{op}();"
    elif op.startswith("checked_"):
        arg = "b" if op in ("checked_ilog", "checked_next_multiple_of") else ""
        expression = f"let v=a.{op}({arg}); flag=u32::from(v.is_none());let value=v.unwrap_or(0);"
    elif op == "cmp":
        expression = "let value=((a>b) as i64)-((a<b) as i64);"
    elif op == "cast":
        destination = {"usize": "AqUsize", "isize": "AqIsize"}.get(case["destination"], case["destination"])
        expression = f"let value=a as {destination};"
    elif op == "mixed":
        expression = f"let mut value=a; for j in 0..64{typ} {{ value=value.rotate_left(5).wrapping_mul(0x9e3779b9).wrapping_add((p.c as {typ}) ^ j.wrapping_mul(0x7f4a7c15)); }}"
    elif op == "loop":
        expression = "let mut value=a; for j in 0..b { value=value.wrapping_add(j); }"
    else:
        raise ValueError(op)
    # Signed values are zero-extended from their destination-width bit pattern.
    outbits = 64 if op == "cmp" else case.get("destination_bits", case["bits"])
    mask = f" & {(1 << outbits) - 1}u128" if outbits < 128 else ""
    raw = f"(value as u128){mask}" if mask else "value as u128"
    return start + expression + f"\nlet raw={raw}; q.lo=raw as u64; q.hi=(raw>>64) as u64; q.flag=flag; q.reserved=0;"


def c_body(case):
    if not case["c_available"]:
        raise ValueError("no native C equivalent")
    bits, signed, op, mode = (case[k] for k in ("bits", "signed", "op", "mode"))
    u, t = f"uint{bits}_t", f"{'int' if signed else 'uint'}{bits}_t"
    mask, upper = (1 << bits) - 1, (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    lo = f"INT{bits}_MIN" if signed else "0"
    hi = f"INT{bits}_MAX" if signed else f"UINT{bits}_MAX"
    lines = [f"{u} ua=({u})p->a, ub=({u})p->b; {t} a=({t})ua, b=({t})ub, value=0; unsigned flag=0;"]
    if op == "copy":
        lines += ["value=a;"]
    elif op in ("add", "sub", "mul"):
        lines += [f"flag=__builtin_{op}_overflow(a,b,&value);"]
        sat = f"(a<0 ? {lo} : {hi})" if signed and op == "add" else f"(a<0 ? {lo} : {hi})" if signed and op == "sub" else f"(((a<0)!=(b<0)) ? {lo} : {hi})" if signed else "0" if op == "sub" else hi
    elif op in ("neg", "abs"):
        if op == "neg":
            lines += [f"flag=__builtin_sub_overflow(({t})0,a,&value);"]
            sat = hi if signed else "0"
        else:
            lines += [f"value=a; if (a<0) flag=__builtin_sub_overflow(({t})0,a,&value);"]
            sat = hi
    elif op == "pow":
        lines += [f"value=1; {t} base=a; uint32_t power=(uint32_t)p->b; while(power){{ if(power&1)flag|=__builtin_mul_overflow(value,base,&value); power>>=1; if(power)flag|=__builtin_mul_overflow(base,base,&base); }}"]
        sat = f"(a<0 && (p->b&1) ? {lo} : {hi})" if signed else hi
    elif op in ("div", "rem", "div_euclid", "rem_euclid"):
        overflow = f"(a=={lo} && b==-1)" if signed else "0"
        lines += [f"flag=!b || {overflow}; if(flag)value={'a' if op.startswith('div') else '0'}; else value=({t})(a{'/' if op.startswith('div') else '%'}b);"]
        if op.endswith("euclid"):
            # Avoid abs(MIN) and negating MIN in signed arithmetic.
            if op.startswith("div"):
                lines += ["if(!flag && a%b<0) value += b>0 ? -1 : 1;"]
            else:
                lines += [f"if(!flag && value<0) value=({t})(({u})value + (b<0 ? ({u})(0-({u})b) : ({u})b));"]
        sat = hi
    elif op in ("shl", "shr"):
        lines += [f"unsigned count=(unsigned)(p->b%{bits}); flag=p->b>={bits}; value=({t})({'(uint64_t)ua<<count' if op == 'shl' else 'a>>count'});"]
        sat = "0"
    elif op in ("and", "or", "xor", "not"):
        expression = "~ua" if op == "not" else f"ua { {'and':'&','or':'|','xor':'^'}[op]} ub"
        lines += [f"value=({t})({expression});"]
    elif op.startswith("rotate_"):
        count = "5" if op.endswith("_const") else f"(unsigned)(p->b%{bits})"
        l, r = ("count", f"(({bits}-count)%{bits})") if op.startswith("rotate_left") else (f"(({bits}-count)%{bits})", "count")
        lines += [f"unsigned count={count}; value=({t})(((uint64_t)ua<<{l}) | ((uint64_t)ua>>{r}));"]
    elif op in ("count_ones", "count_zeros", "leading_zeros", "trailing_zeros", "leading_ones", "trailing_ones"):
        source = f"({u})~ua" if op.endswith("ones") and op != "count_ones" else "ua"
        lines += [f"uint64_t v={source};"]
        expression = "__builtin_popcountll(v)" if op == "count_ones" else f"{bits}-__builtin_popcountll(v)" if op == "count_zeros" else f"v ? __builtin_clzll(v)-{64-bits} : {bits}" if op.startswith("leading_") else f"v ? __builtin_ctzll(v) : {bits}"
        lines += [f"value=({t})({expression});"]
    elif op in ("swap_bytes", "reverse_bits"):
        step = "8" if op == "swap_bytes" else "1"
        unitmask = "255" if op == "swap_bytes" else "1"
        lines += [f"{u} source=ua, dest=0; for(unsigned j=0;j<{bits};j+={step}){{dest=({u})(((uint64_t)dest<<{step}) | (source&{unitmask}));source=({u})((uint64_t)source>>{step});}} value=({t})dest;"]
    elif op in ("min", "max"):
        lines += [f"value=a{'<' if op == 'min' else '>'}b ? a:b;"]
    elif op in ("eq", "ne", "lt", "le", "gt", "ge"):
        lines += [f"value=a { {'eq':'==','ne':'!=','lt':'<','le':'<=','gt':'>','ge':'>='}[op]} b;"]
    elif op == "abs_diff":
        lines += [f"value=({t})(a>b ? ({u})(({u})a-({u})b) : ({u})(({u})b-({u})a));"]
    elif op in ("isqrt", "checked_isqrt"):
        lines += [f"{u} remainder=ua, root=0, bit=({u})1 << {(bits-1)//2*2};"]
        if signed: lines += ["flag=a<0;"]
        lines += [f"while(bit>remainder)bit>>=2;while(bit){{if(remainder>=root+bit){{remainder-=root+bit;root=(root>>1)+bit;}}else root>>=1;bit>>=2;}}value=({t})root;"]
        if signed: lines += ["if(flag)value=0;"]
    elif op.startswith("checked_ilog"):
        base = "b" if op == "checked_ilog" else "2" if op == "checked_ilog2" else "10"
        lines += [f"{t} base={base};flag=a<=0 || base<=1;{u} v=ua;value=0;if(!flag)while(v>=({u})base){{v/=({u})base;++value;}}"]
    elif op == "checked_next_power_of_two":
        lines += [f"flag=ua>(({u})1 << {bits-1});{u} v=1;if(!flag)while(v<ua)v=({u})(v<<1);value=flag ? 0:({t})v;"]
    elif op in ("div_ceil", "checked_next_multiple_of", "is_multiple_of"):
        if op == "is_multiple_of": lines += ["value=ub ? ua%ub==0 : ua==0;"]
        else:
            lines += [f"flag=!ub;{u} v=ub ? ua/ub + (ua%ub!=0):0;"]
            if op == "checked_next_multiple_of": lines += ["flag|=__builtin_mul_overflow(v,ub,&value);if(flag)value=0;"]
            else: lines += [f"value=({t})v;"]
    elif op == "signum":
        lines += ["value=(a>0)-(a<0);"]
    elif op == "cmp":
        lines += ["q->lo=(uint64_t)((int64_t)(a>b)-(int64_t)(a<b)); q->hi=0; q->flag=0; q->reserved=0; continue;"]
    elif op == "cast":
        lines += [f"q->lo=(uint64_t)(uint{case['destination_bits']}_t)a; q->hi=0; q->flag=0; q->reserved=0; continue;"]
    elif op == "mixed":
        lines += [f"{u} v=ua; for({u} j=0;j<64;++j) v=({u})((({u})(((uint64_t)v<<5)|((uint64_t)v>>{bits-5})) * UINT64_C(0x9e3779b9)) + (({u})p->c ^ ({u})(j*UINT64_C(0x7f4a7c15)))); value=({t})v;"]
    elif op == "loop":
        lines += [f"{u} v=ua; for({u} j=0;j<ub;++j) v=({u})(v+j);value=({t})v;"]
    else:
        raise ValueError(op)
    if mode == "checked": lines += ["if(flag)value=0;"]
    elif mode == "saturating": lines += [f"if(flag)value={sat};"]
    if mode not in ("checked", "overflowing") and not op.startswith("checked_"): lines += ["flag=0;"]
    lines += [f"q->lo=(uint64_t)({u})value; q->hi=0; q->flag=flag; q->reserved=0;"]
    return "\n".join(lines)
