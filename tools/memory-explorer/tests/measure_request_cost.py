#!/usr/bin/env python3
"""Measure six ARM production translation units; never build or run firmware.

Compare the entry-only and paired allocator patches with tracing enabled and
compiled out. Private source/configuration overlays borrow real native headers
from a prepared, read-only NuttX context. Object sections and GCC stack frames
are not final-link costs, call-chain bounds, peak stack use, or timing evidence.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess

from build_requests_nuttx import PATCH, patch_paths
from build_timing_nuttx import NUTTX_REVISION, validate_configuration


BASELINE = "604975b086d0dad5ac3a973190ebcb283b1c875e"
UNITS = ("mm/mm_heap/mm_malloc.c", "mm/mm_heap/mm_memalign.c", "mm/mm_heap/mm_realloc.c",
         "drivers/note/note_driver.c", "drivers/note/noteram_driver.c", "fs/procfs/fs_procfsmeminfo.c")
PATHS = sorted((*UNITS, "include/nuttx/mm/mm.h", "include/nuttx/sched_note.h",
                "mm/mm_heap/mm.h", "mm/Kconfig"))
OWNED = {"g_mm_request_id", "mm_request_begin", "mm_request_end", "mm_alloc_requests",
         "sched_note_heap_request", "sched_note_heap_result"}
CATEGORIES = ("executable", "readonly", "unwind", "writable", "nobits", "unsupported")
LAYOUT_FIELDS = ("pointer_bytes", "heap_bytes", "counter_field_bytes", "counter_offset",
                 "common_note_bytes", "request_note_bytes", "result_note_bytes",
                 "aligned_request_bytes", "aligned_result_bytes", "note_buffer_bytes", "max_drivers")
FLAGS = ["-Wstrict-prototypes", "-Wno-attributes", "-Wno-unknown-pragmas", "-Wno-psabi",
         "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
         "-fno-omit-frame-pointer", "-fno-optimize-sibling-calls", "-funwind-tables",
         "-fasynchronous-unwind-tables", "--param=min-pagesize=0", "-fno-common",
         "-Wall", "-Wshadow", "-Wundef", "-ffunction-sections", "-fdata-sections", "-g",
         "-mlittle-endian", "-march=armv8-m.main", "-mtune=cortex-m33", "-mfloat-abi=soft",
         "-mthumb", "-Wa,-mthumb", "-Wa,-mimplicit-it=always", "-D__NuttX__"]
LAYOUT_SOURCE = """#include <stddef.h>
#include <stdint.h>
#include <nuttx/config.h>
#include <nuttx/sched_note.h>
#include "mm_heap/mm.h"
#ifdef CONFIG_MM_ALLOC_REQUEST_TRACE
# define COUNTER_SIZE sizeof(((struct mm_heap_s *)0)->mm_alloc_requests)
# define COUNTER_OFFSET offsetof(struct mm_heap_s, mm_alloc_requests)
# if NXRS_COST_PAIRED
#  define REQUEST_SIZE sizeof(struct note_heap_request_s)
#  define RESULT_SIZE sizeof(struct note_heap_result_s)
# else
#  define REQUEST_SIZE sizeof(struct note_heap_s)
#  define RESULT_SIZE 0
# endif
#else
# define COUNTER_SIZE 0
# define COUNTER_OFFSET 0
# define REQUEST_SIZE 0
# define RESULT_SIZE 0
#endif
const uint32_t nxrs_request_cost_layout[] = {
  sizeof(uintptr_t), sizeof(struct mm_heap_s), COUNTER_SIZE, COUNTER_OFFSET,
  sizeof(struct note_common_s), REQUEST_SIZE, RESULT_SIZE,
  NOTE_ALIGN(REQUEST_SIZE), NOTE_ALIGN(RESULT_SIZE),
  CONFIG_DRIVERS_NOTERAM_BUFSIZE, CONFIG_DRIVERS_NOTE_MAX
};
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes(paths):
    return {str(Path(path).resolve()): digest(path) for path in sorted(set(map(Path, paths)))}


def unchanged(expected):
    require(all(Path(path).is_file() and digest(path) == value for path, value in expected.items()),
            "A recorded input changed during measurement")


def merge_inputs(retained, current):
    for path, value in current.items():
        require(path not in retained or retained[path] == value,
                f"A shared input changed between compilation units: {path}")
    retained.update(current)


class Commands:
    def __init__(self, output):
        self.output, self.commands = output, []
        self.environment = dict(os.environ)
        for name in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH",
                     "GCC_EXEC_PREFIX", "COMPILER_PATH", "LIBRARY_PATH", "DEPENDENCIES_OUTPUT",
                     "SUNPRO_DEPENDENCIES"):
            self.environment.pop(name, None)

    def run(self, command, cwd, timeout=60, *, environment=None):
        command = list(map(str, command))
        index = len(self.commands)
        record = {"argv": command, "cwd": str(cwd), "timeout_seconds": timeout,
                  "stdout": f"commands/{index:03}.stdout", "stderr": f"commands/{index:03}.stderr"}
        if environment:
            record["environment_overrides"] = environment
        self.commands.append(record)
        try:
            result = subprocess.run(command, cwd=cwd, env=self.environment | (environment or {}),
                                    capture_output=True,
                                    timeout=timeout, check=False)
            record["returncode"] = result.returncode
            (self.output / record["stdout"]).write_bytes(result.stdout)
            (self.output / record["stderr"]).write_bytes(result.stderr)
            require(result.returncode == 0, f"Command {index} failed ({result.returncode}); see {record['stderr']}")
            return result.stdout
        except subprocess.TimeoutExpired as error:
            record["timed_out"] = True
            (self.output / record["stdout"]).write_bytes(error.stdout or b"")
            (self.output / record["stderr"]).write_bytes(error.stderr or b"")
            raise RuntimeError(f"Command {index} exceeded {timeout} seconds") from error
        finally:
            (self.output / "commands.json").write_text(json.dumps(self.commands, indent=2) + "\n")

    def apply_patch(self, directory, patch, *, reverse=False):
        command = ["git", "apply"]
        if reverse:
            command.append("--reverse")
        # Git otherwise discovers the enclosing checkout and skips paths
        # outside the scratch directory's repository-relative prefix.
        environment = {"GIT_CEILING_DIRECTORIES": str(directory.parent)}
        self.run([*command, "--check", patch], directory, 30, environment=environment)
        self.run([*command, patch], directory, 30, environment=environment)


def read_patch(commands, repo, ref):
    revision = commands.run(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                            repo, 30).decode().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Expected one exact source commit")
    content = commands.run(["git", "show", revision + ":" + PATCH], repo, 30)
    paths = patch_paths(content.decode())
    require(set(paths) <= set(PATHS), "Patch changes sources outside the six-unit cost witness")
    return {"revision": revision, "sha256": hashlib.sha256(content).hexdigest(), "paths": paths}, content


def admit_context(context, ledger, patches):
    require(ledger.get("upstream_revision") == NUTTX_REVISION, "Context has the wrong NuttX pin")
    records = [p for p in ledger.get("patches", []) if p.get("name") == Path(PATCH).name]
    require(len(records) == 1, "Context must record exactly one production recorder patch")
    record = records[0]
    admitted = [name for name, patch in patches.items() if patch["sha256"] == record.get("sha256")]
    require(len(admitted) == 1, "Context recorder patch is neither the baseline nor the candidate")
    selected = patches[admitted[0]]
    require(sorted(record.get("files", {})) == selected["paths"], "Context patch file inventory differs")
    for name, identity in record["files"].items():
        require(digest(context / name) == identity["after"], f"Context patched source differs: {name}")
    return admitted[0], record


def cost_configuration(text, enabled):
    validate_configuration(text, {"ARCH_BOARD_MPS2_AN521": "y", "BUILD_FLAT": "y",
        "MM_DEFAULT_MANAGER": "y", "SMP": "n", "ARCH_FPU": "n", "MM_HEAP_MEMPOOL": "n",
        "MM_TLSF_MANAGER": "n", "MM_KERNEL_HEAP": "n", "SCHED_INSTRUMENTATION": "y",
        "SCHED_INSTRUMENTATION_HEAP": "y", "SCHED_INSTRUMENTATION_FILTER": "y",
        "DRIVERS_NOTERAM": "y", "DRIVERS_NOTERAM_DEFAULT_NOOVERWRITE": "y",
        "DRIVERS_NOTE_MAX": "1", "DEBUG_NOOPT": "y", "LTO_NONE": "y"})
    replacements = {"EXAMPLES_HEAP_REQUESTS": "n", "MM_ALLOC_REQUEST_TRACE": "y" if enabled else "n"}
    lines = [line for line in text.splitlines() if not any(
        line.startswith("CONFIG_" + key + "=") or line == "# CONFIG_" + key + " is not set"
        for key in replacements)]
    lines.extend("CONFIG_" + key + "=" + value if value != "n" else "# CONFIG_" + key + " is not set"
                 for key, value in replacements.items())
    return "\n".join(lines) + "\n"


def dependencies(path, cwd):
    text = path.read_text().replace("\\\n", " ")
    require(":" in text, "Compiler did not produce a dependency rule")
    return sorted({(cwd / name).resolve() for name in shlex.split(text.partition(":")[2])})


def read_elf(path):
    """Read the narrow ELF32 little-endian ARM relocatable format we compile."""
    data = Path(path).read_bytes()
    require(len(data) >= 52 and data[:7] == b"\x7fELF\x01\x01\x01", "Expected ELF32 little-endian object")
    require(struct.unpack_from("<HH", data, 16) == (1, 40), "Expected a relocatable ARM object")
    offset = struct.unpack_from("<I", data, 32)[0]
    width, count, names_index = struct.unpack_from("<HHH", data, 46)
    require(width == 40 and 0 < count < 0xff00 and names_index < count and
            offset + count * width <= len(data), "Invalid ELF section table")
    sections = []
    for index in range(count):
        name, kind, flags, address, start, size, link, info, align, entry = struct.unpack_from(
            "<10I", data, offset + index * width)
        require(kind == 8 or start + size <= len(data), "Truncated ELF section")
        sections.append(dict(name_offset=name, type=kind, flags=flags, offset=start, size=size,
                             link=link, alignment=align, entry_size=entry))

    def section_bytes(section):
        return data[section["offset"]:section["offset"] + section["size"]]

    def string(table, index):
        require(0 <= index < len(table) and b"\0" in table[index:], "Invalid ELF string")
        return table[index:table.index(b"\0", index)].decode("utf-8")

    names = section_bytes(sections[names_index])
    totals = dict.fromkeys(CATEGORIES, 0)
    allocated = []
    for section in sections:
        section["name"] = string(names, section["name_offset"])
        if not section["flags"] & 2:
            continue
        flags, kind, name = section["flags"], section["type"], section["name"]
        if flags & 0x400:  # TLS is outside this fixture's admitted static storage.
            category = "unsupported"
        elif kind == 8:
            category = "nobits"
        elif name.startswith((".ARM.exidx", ".ARM.extab")):
            category = "unwind"
        elif kind != 1:
            category = "unsupported"
        elif flags & 4:
            category = "executable"
        elif flags & 1:
            category = "writable"
        else:
            category = "readonly"
        totals[category] += section["size"]
        allocated.append({key: section[key] for key in ("name", "type", "flags", "size", "alignment")}
                         | {"category": category})
    symbols = []
    for section in sections:
        if section["type"] != 2:
            continue
        require(section["entry_size"] == 16 and section["size"] % 16 == 0 and
                section["link"] < count, "Invalid ELF symbol table")
        strings = section_bytes(sections[section["link"]])
        for start in range(section["offset"], section["offset"] + section["size"], 16):
            name, value, size, info, other, index = struct.unpack_from("<IIIBBH", data, start)
            name = string(strings, name)
            if name:
                symbols.append({"name": name, "value": value, "size": size,
                                "type": info & 15, "section_index": index})
    return {"totals": totals, "allocated_sections": allocated, "symbols": symbols,
            "sections": sections, "data": data}


def read_layout(elf):
    symbols = [s for s in elf["symbols"] if s["name"] == "nxrs_request_cost_layout"]
    require(len(symbols) == 1 and symbols[0]["size"] == 4 * len(LAYOUT_FIELDS), "Missing target layout witness")
    symbol = symbols[0]
    require(0 < symbol["section_index"] < len(elf["sections"]), "Layout witness is not defined")
    section = elf["sections"][symbol["section_index"]]
    require(section["type"] == 1 and symbol["value"] + symbol["size"] <= section["size"], "Invalid layout extent")
    values = struct.unpack_from("<" + "I" * len(LAYOUT_FIELDS), elf["data"], section["offset"] + symbol["value"])
    return dict(zip(LAYOUT_FIELDS, values))


def stack_usage(path):
    require(path.is_file(), "Compiler did not produce promised stack-usage evidence")
    rows = []
    for line in path.read_text().splitlines():
        parts = line.rsplit("\t", 2)
        require(len(parts) == 3 and parts[1].isdigit(), "Malformed GCC stack-usage row")
        location = parts[0].rsplit(":", 3)
        require(len(location) == 4 and location[1].isdigit() and location[2].isdigit(), "Missing stack function location")
        require(parts[2] in ("static", "dynamic", "dynamic,bounded"), "Unknown GCC stack qualifier")
        rows.append({"function": location[3], "source": location[0], "line": int(location[1]),
                     "bytes": int(parts[1]), "qualifier": parts[2]})
    require(rows, "Compiler produced an empty stack-usage report")
    return rows


def bind_stack_symbols(symbols, rows):
    """GCC .su omits numeric suffixes on its optimized IPA clones."""
    bound = [dict(row, symbol=None) for row in rows]
    for symbol in symbols:
        if symbol["type"] != 2 or symbol["section_index"] == 0:
            continue
        name = symbol["name"]
        spelling = re.sub(r"\.(isra|constprop|part)\.[0-9]+(?=\.|$)", r".\1", name)
        matches = [row for row in bound if row["function"] in (name, spelling)]
        require(len(matches) == 1 and matches[0]["symbol"] is None,
                f"Missing or ambiguous stack-usage row for compiled function: {name}")
        matches[0]["symbol"] = name
    return bound


def compile_unit(commands, compiler, context, overlay, source, target, optimization, paired=None):
    flags = FLAGS + ["-" + optimization, "-fstack-usage", "-ffile-prefix-map=" + str(overlay) + "=/nxrs-cost",
        "-isystem", str(overlay / "include"), "-isystem", str(context / "include"),
        "-I", str(overlay / "mm"), "-I", str(context / "mm"), "-I", str(context),
        "-I", str(context / "sched"), "-I", str(context / "fs"), "-I", str(context / "drivers/note")]
    if paired is not None:
        flags += ["-DNXRS_COST_PAIRED=" + str(int(paired))]
    depfile = target.with_suffix(".d")
    commands.run([compiler, *flags, "-M", "-MF", depfile, "-MT", target.name, source], overlay)
    inputs = dependencies(depfile, overlay)
    require((overlay / "include/nuttx/config.h").resolve() in inputs, "Private configuration was not consumed")
    require(not any((context / name).resolve() in inputs for name in PATHS if name.endswith(".h")),
            "Compiler bypassed a private patched header")
    before = hashes(inputs)
    commands.run([compiler, *flags, "-MD", "-MF", depfile, "-c", source, "-o", target], overlay)
    require(dependencies(depfile, overlay) == inputs, "Compiler dependency set changed during measurement")
    unchanged(before)
    return before


def compare(variants, after, before):
    a, b = variants[after], variants[before]
    return {"after": after, "before": before,
            "allocated_section_bytes": {key: a["totals"][key] - b["totals"][key] for key in CATEGORIES},
            "heap_struct_bytes": a["layout"]["heap_bytes"] - b["layout"]["heap_bytes"],
            "global_id_bytes": a["global_id_bytes"] - b["global_id_bytes"],
            "per_unit": {unit: {key: a["units"][unit]["totals"][key] - b["units"][unit]["totals"][key]
                                for key in CATEGORIES} for unit in UNITS}}


def recorder_storage(units, layout, family, enabled):
    symbols = [s for unit in units.values() for s in unit["recorder_symbols"]]
    defined = [s for s in symbols if s["section_index"] != 0]
    expected = (OWNED if family == "paired" else {"mm_alloc_requests"}) if enabled else set()
    require({s["name"] for s in defined} == expected and len(defined) == len(expected),
            "Recorder symbol admission differs")
    require(enabled or not symbols, "Compile-disabled code still references recorder symbols")
    ids = [s for s in defined if s["name"] == "g_mm_request_id"]
    global_bytes = sum(s["size"] for s in ids)
    require(global_bytes == (8 if family == "paired" and enabled else 0), "Unexpected global ID storage")
    require(layout["pointer_bytes"] == 4 and layout["counter_field_bytes"] == (8 if enabled else 0),
            "Unexpected native pointer/counter layout")
    return global_bytes


def measure(args, commands):
    repo = Path(__file__).resolve().parents[3]
    context, output = args.nuttx_context.resolve(), args.output.resolve()
    harness_hashes = hashes([Path(__file__), Path(__file__).with_name("build_requests_nuttx.py"),
                            Path(__file__).with_name("build_timing_nuttx.py")])
    require(not output.is_relative_to(context), "Output must be outside the read-only source context")
    patches, patch_files = {}, {}
    for name, ref in (("entry", args.baseline_ref), ("paired", args.candidate_ref)):
        patches[name], content = read_patch(commands, repo, ref)
        patch_files[name] = output / (name + ".patch")
        patch_files[name].write_bytes(content)
    require(set(patches["entry"]["paths"]) | set(patches["paired"]["paths"]) == set(PATHS),
            "Recorder patch inventory does not cover the admitted units and headers")
    guard = hashes([context / path for path in PATHS] + [args.config, args.context_receipt,
                    context / ".config", context / "include/nuttx/config.h"])
    ledger = json.loads(args.context_receipt.read_text())
    selected, context_patch = admit_context(context, ledger, patches)
    pristine = output / "pristine"
    for path in PATHS:
        (pristine / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(context / path, pristine / path)
    commands.apply_patch(pristine, patch_files[selected], reverse=True)
    require(all(digest(pristine / path) == identity["before"] for path, identity in context_patch["files"].items()),
            "Reconstructed original source differs from the context patch ledger")
    original = {path: digest(pristine / path) for path in PATHS}

    compiler = Path(shutil.which(args.tool_prefix + "gcc") or args.tool_prefix + "gcc").resolve()
    require(compiler.is_file(), "Missing ARM compiler")
    version = commands.run([compiler, "--version"], output, 30).decode().splitlines()[0]
    require(commands.run([compiler, "-dumpmachine"], output, 30).decode().strip() == "arm-none-eabi",
            "Expected the ARM bare-metal compiler")
    tool_paths = [compiler]
    for tool in ("cc1", "as"):
        path = commands.run([compiler, "-print-prog-name=" + tool], output, 30).decode().strip()
        tool_paths.append(Path(shutil.which(path) or path).resolve())
    tool_hashes = hashes(tool_paths)
    tools = output / "host-tools"
    tools.mkdir()
    mkconfig_inputs = [context / "tools" / name for name in ("mkconfig.c", "cfgdefine.c", "cfgdefine.h")]
    guard.update(hashes(mkconfig_inputs))
    for path in mkconfig_inputs:
        shutil.copyfile(path, tools / path.name)
    hostcc = shutil.which(args.host_cc)
    require(hostcc is not None, "Missing host C compiler for native mkconfig")
    tool_hashes.update(hashes([hostcc]))
    commands.run([hostcc, "-std=gnu11", "-O2", "mkconfig.c", "cfgdefine.c", "-o", "mkconfig"], tools)
    variants, all_dependencies = {}, {}
    for family in ("entry", "paired"):
        for enabled in (False, True):
            name = family + ("_enabled" if enabled else "_disabled")
            directory = output / name
            overlay = directory / "nuttx"
            shutil.copytree(pristine, overlay)
            commands.apply_patch(overlay, patch_files[family])
            configuration = cost_configuration(args.config.read_text(), enabled)
            (overlay / ".config").write_text(configuration)
            config_header = overlay / "include/nuttx/config.h"
            config_header.write_bytes(commands.run([tools / "mkconfig", overlay], tools))
            objects = directory / "objects"
            objects.mkdir()
            units, totals = {}, dict.fromkeys(CATEGORIES, 0)
            for source in UNITS:
                target = objects / (Path(source).stem + ".o")
                dependencies_used = compile_unit(commands, compiler, context, overlay, source, target, args.optimization)
                merge_inputs(all_dependencies, dependencies_used)
                elf = read_elf(target)
                usage = bind_stack_symbols(elf["symbols"], stack_usage(target.with_suffix(".su")))
                units[source] = {"object": str(target.relative_to(output)), "sha256": digest(target),
                    "totals": elf["totals"], "allocated_sections": elf["allocated_sections"],
                    "recorder_symbols": [s for s in elf["symbols"] if s["name"] in OWNED],
                    "stack_usage": usage, "dependencies": dependencies_used}
                for category in CATEGORIES:
                    totals[category] += elf["totals"][category]
            (overlay / "layout.c").write_text(LAYOUT_SOURCE)
            target = objects / "layout.o"
            merge_inputs(all_dependencies, compile_unit(commands, compiler, context, overlay, "layout.c", target,
                                                        args.optimization, paired=family == "paired"))
            layout = read_layout(read_elf(target))
            global_bytes = recorder_storage(units, layout, family, enabled)
            variants[name] = {"trace_enabled": enabled, "patch": family, "config_sha256": digest(overlay / ".config"),
                "config_header_sha256": digest(config_header), "source_sha256": {p: digest(overlay / p) for p in PATHS},
                "units": units, "totals": totals, "layout": layout, "global_id_bytes": global_bytes,
                "layout_object_sha256": digest(target)}
    unchanged(guard)
    unchanged(tool_hashes)
    unchanged(all_dependencies)
    unchanged(harness_hashes)
    require(variants["paired_enabled"]["layout"]["heap_bytes"] == variants["entry_enabled"]["layout"]["heap_bytes"],
            "Paired recorder unexpectedly changes the enabled heap layout")
    return {"schema_version": 1, "evidence": "arm_allocator_recorder_translation_unit_cost", "passed": True,
        "scope": "Six complete production C translation units; no final link or runtime execution",
        "nuttx_revision": NUTTX_REVISION, "context": str(context), "context_recorder_patch": selected,
        "patches": patches, "original_sources": original, "input_sha256": guard, "compiler": version,
        "compiler_inputs_sha256": tool_hashes, "optimization": args.optimization,
        "harness_sha256": harness_hashes,
        "compiler_policy": {"kind": "explicit recorded object-measurement flags, not final firmware build flags",
                            "common_flags": FLAGS + ["-" + args.optimization, "-fstack-usage"]},
        "configuration_normalization": {"EXAMPLES_HEAP_REQUESTS": "n", "MM_ALLOC_REQUEST_TRACE": "per variant"},
        "variants": variants,
        "comparisons": {"entry_enabled_cost": compare(variants, "entry_enabled", "entry_disabled"),
            "paired_enabled_cost": compare(variants, "paired_enabled", "paired_disabled"),
            "paired_increment": compare(variants, "paired_enabled", "entry_enabled"),
            "disabled_control_drift": compare(variants, "paired_disabled", "entry_disabled")},
        "limits": ["Executable section bytes include literal pools; debug and relocation sections are excluded",
            "Unsupported allocated sections are retained separately, not silently discarded",
            "Object sums omit final-link reachability, alignment and effects in other translation units; not a firmware upper bound",
            "Counter field bytes and heap-structure growth differ when ABI padding changes",
            "GCC stack usage is per-function compiler evidence, not call-chain bounds or measured high-water",
            "Record sizes describe this ABI; actual emitted volume depends on filtering, nested calls and loss",
            "Request/result bytes exclude ordinary heap notes, markers and other concurrent events",
            "Runtime-filtered calls still update counters/IDs; they are not the compile-disabled control",
            "The note ring reservation is unchanged; larger records consume its existing capacity",
            "No timing, interrupt/SMP cost, physical-board or full-firmware overhead qualification"]}


def run(args):
    args.output = args.output.resolve()
    require(not args.output.is_relative_to(args.nuttx_context.resolve()), "Output must be outside the read-only source context")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "commands").mkdir()
    commands = Commands(args.output)
    try:
        report = measure(args, commands)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(args.output / "report.json"), "comparisons": {
            name: value["allocated_section_bytes"] for name, value in report["comparisons"].items()}}))
        return report
    except Exception as error:
        (args.output / "failure.json").write_text(json.dumps({"error": str(error)}, indent=2) + "\n")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("nuttx-context", "context-receipt", "config", "output"):
        parser.add_argument("--" + argument, type=Path, required=True)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--candidate-ref", default="HEAD")
    parser.add_argument("--tool-prefix", default="arm-none-eabi-")
    parser.add_argument("--host-cc", default="cc")
    parser.add_argument("--optimization", choices=("O0", "Os"), default="O0")
    run(parser.parse_args())
