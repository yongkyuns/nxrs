"""Inspect the pinned NuttX Kconfig model in Make's olddefconfig environment."""
import json
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys

import kconfiglib as kc


def entries(path):
    result = {}
    for line in path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.removeprefix("CONFIG_").split("=", 1)
            result[name] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            result[line.removeprefix("# CONFIG_").removesuffix(" is not set")] = "n"
    return result


def number(value, base):
    try:
        return int(value, base)
    except ValueError:
        return None


def decode(symbol, value):
    kind = symbol.orig_type
    if kind == kc.STRING:
        if value.startswith('"') and value.endswith('"'):
            return kc.unescape(value[1:-1])
    elif kind == kc.INT:
        return number(value, 10)
    elif kind == kc.HEX:
        result = number(value, 16)
        return result if result is not None and result >= 0 else None
    elif kind in (kc.BOOL, kc.TRISTATE):
        legal = ("n", "y") if symbol.type == kc.BOOL else ("n", "m", "y")
        return value if value in legal else None
    return None


def symbol_name(item):
    if isinstance(item, kc.Symbol) and item.nodes:
        return f"CONFIG_{item.name}"
    return kc.standard_sc_expr_str(item)


def condition(expr):
    return {"expression": kc.expr_str(expr, symbol_name), "value": kc.TRI_TO_STR[kc.expr_value(expr)]}


@lru_cache(maxsize=1)
def apps_root():
    return Path(os.environ.get("APPSDIR", "../apps")).resolve()


def locations(symbol):
    for node in symbol.nodes:
        ancestors = []
        parent = node.parent
        while parent and parent.parent:
            if parent.prompt:
                ancestors.append(parent.prompt[0])
            parent = parent.parent
        filename = Path(node.filename)
        if filename.is_absolute() and filename.is_relative_to(apps_root()):
            filename = Path("apps") / filename.relative_to(apps_root())
        yield {
            "file": str(filename),
            "line": node.linenr,
            "menu": list(reversed(ancestors)),
            "prompt": node.prompt[0] if node.prompt else None,
            "prompt_condition": condition(node.prompt[1]) if node.prompt else None,
            "help": node.help,
            "requires": condition(node.dep),
        }


def describe(symbol, selectors, implications):
    definition = {
        "type": kc.TYPE_TO_STR[symbol.type],
        "effective": symbol.str_value,
        "visibility": kc.TRI_TO_STR[symbol.visibility],
        "assignable": [kc.TRI_TO_STR[value] for value in symbol.assignable],
        "locations": list(locations(symbol)),
        "requires": condition(symbol.direct_dep),
        "prerequisites": sorted(
            ref.name for ref in kc.expr_items(symbol.direct_dep)
            if isinstance(ref, kc.Symbol) and ref.nodes
        ),
        "enabled_by": selectors.get(symbol.name, []),
        "implied_by": implications.get(symbol.name, []),
        "defaults": [
            {"value": kc.expr_str(value, symbol_name), "when": condition(when)}
            for value, when in symbol.defaults
        ],
        "ranges": [
            {"min": low.str_value, "max": high.str_value, "when": condition(when)}
            for low, high, when in symbol.ranges
        ],
    }
    if symbol.choice:
        choice = symbol.choice
        definition["choice"] = {
            "name": choice.name,
            "locations": list(locations(choice)),
            "alternatives": [option.name for option in choice.syms],
            "selected": choice.selection.name if choice.selection else None,
        }
    return definition


def sources(symbols, property_name):
    reverse = {}
    for source in symbols:
        for selected, expr in getattr(source, property_name):
            reverse.setdefault(selected.name, []).append({
                "symbol": source.name,
                "value": source.str_value,
                "when": condition(expr),
            })
    return reverse


def check_requests(model, requests):
    problems = []
    promoted_choices = {
        symbol.choice for name, history in requests.items()
        if (symbol := model.syms.get(name)) and symbol.choice
        and history[-1].get("command_line") and history[-1]["value"] == "y"
    }
    for name, history in requests.items():
        symbol = model.syms.get(name)
        for index, request in enumerate(history):
            value, origin = request["value"], request["source"]
            error = None
            if not symbol or not symbol.nodes:
                error = "unknown symbol"
            elif decode(symbol, value) is None:
                error = f"invalid {kc.TYPE_TO_STR[symbol.type]} value"
            elif (not request.get("command_line") and value == "y"
                  and symbol.choice in promoted_choices):
                # CLI assignment replaces the fragment selection of this choice.
                continue
            elif index == len(history) - 1:
                current = symbol.str_value
                if symbol.orig_type == kc.STRING:
                    current = '"' + kc.escape(current) + '"'
                if decode(symbol, value) != decode(symbol, current):
                    dependency = condition(symbol.direct_dep)
                    reasons = [f"requires {dependency['expression']} ({dependency['value']})"]
                    if symbol.visibility == 0:
                        reasons.append("no visible prompt")
                    if symbol.choice and symbol.choice.selection:
                        reasons.append(f"choice selects CONFIG_{symbol.choice.selection.name}")
                    for low, high, when in symbol.ranges:
                        if kc.expr_value(when):
                            reasons.append(f"active range {low.str_value}–{high.str_value}")
                            break
                    error = f"effective {current!r}; " + ", ".join(reasons)
            if error:
                problems.append(f"{origin}: CONFIG_{name} requested {value!r}; {error}")
    return problems


def main():
    if kc.VERSION != (14, 1, 0):
        raise SystemExit("Nxrs requires kconfiglib==14.1.0")
    request_path = Path(os.environ["NXRS_KCONFIG_REQUESTS"])
    query = json.loads(request_path.read_text())
    seed = request_path.with_name("seed.config")
    seed.write_bytes(Path(".config").read_bytes())
    # Resolve independently from the input fed to native olddefconfig. Loading
    # its output alone could hide a discarded request or a dependency mismatch.
    native = subprocess.run([sys.executable, "-m", "olddefconfig"], check=True,
                            text=True, capture_output=True)
    model = kc.Kconfig(warn_to_stderr=False)
    model.warn_assign_redun = False
    model.load_config(str(seed))
    expected = seed.with_name("model.config")
    model.write_config(str(expected), save_old=False)
    resolved = entries(Path(".config"))
    metadata_result = entries(expected)
    if resolved != metadata_result:
        mismatch = sorted(key for key in resolved.keys() | metadata_result.keys()
                          if resolved.get(key) != metadata_result.get(key))
        raise SystemExit("Kconfig model and native olddefconfig disagree on " + ", ".join(mismatch[:12]))

    symbols = model.unique_defined_syms
    requests = query["requests"]
    selectors = sources(symbols, "selects") if query["catalog"] else {}
    implications = sources(symbols, "implies") if query["catalog"] else {}
    descriptions = {
        symbol.name: describe(symbol, selectors, implications)
        for symbol in symbols
        if query["catalog"] or symbol.name in requests
    }
    # NuttX accepts repeated assignments and rejects other native warnings.
    warnings = [line for line in native.stderr.splitlines()
                if line and "set more than once" not in line]
    warnings.extend(check_requests(model, requests))
    Path(os.environ["NXRS_KCONFIG_REPORT"]).write_text(json.dumps({
        "backend": "kconfiglib 14.1.0",
        "symbols": descriptions,
        "problems": warnings,
    }))


if __name__ == "__main__":
    main()
