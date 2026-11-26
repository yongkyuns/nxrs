"""Export a stopped NuttX default-allocator snapshot for the memory explorer.

In the target's GDB session, after loading the exact final ELF and stopping all
CPUs at a quiescent point:

    source external/nuttx/tools/pynuttx/gdbinit.py
    source tools/memory-explorer/gdb_export.py
    nxrs-memory-export /tmp/ready.json --label ready --time-ns 0

The NuttX pynuttx dependencies must already be installed. This adapter supports
the default mm_heap layout at NuttX 2b5c0de5e933281c209f954a63b99cf10c132346.
TLSF and pool-slot occupancy need their own adapters. No inferior function is
called, and deferred frees are not drained. A structurally valid stopped heap
is not proof that execution stopped at an allocator transaction boundary.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


MAX_HEAPS = 128
MAX_NODES = 262144
MAX_TASKS = 65536
MAX_REGIONS = 128
MAX_CPUS = 256
U64_MAX = (1 << 64) - 1


class ExportError(Exception):
    """The requested capture cannot be represented truthfully."""


def _fields(value_or_type):
    typ = getattr(value_or_type, "type", value_or_type).strip_typedefs()
    return {field.name for field in typ.fields() if field.name}


def _array_length(value):
    low, high = value.type.strip_typedefs().range()
    if low != 0 or high < low:
        raise ExportError("Unsupported array bounds in target metadata")
    return int(high + 1)


def _extent(start, size, address_limit):
    if start <= 0 or size <= 0 or start + size > address_limit:
        raise ExportError(f"Invalid target extent: {start:#x} + {size:#x}")
    return start + size


def require_stopped(gdb_api):
    """Check every thread GDB exposes, including other connected inferiors."""
    selected = gdb_api.selected_inferior()
    if not selected or not selected.threads():
        raise ExportError("Select a stopped target inferior with visible threads")
    observed = []
    for inferior in gdb_api.inferiors():
        for thread in inferior.threads():
            if not thread.is_stopped():
                raise ExportError(
                    f"Inferior {inferior.num} has a running or exited thread; "
                    "stop all target CPUs before exporting"
                )
            observed.append({"inferior": int(inferior.num), "thread": int(thread.num)})
    return observed


def elf_identity(filename):
    """Hash host file bytes; this does not verify the running target image."""
    if not filename:
        raise ExportError("Load the exact final ELF with GDB's file command first")
    path = Path(filename).expanduser().resolve()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        header = stream.read(16)
        if len(header) != 16 or header[:4] != b"\x7fELF" or header[4] not in (1, 2):
            raise ExportError(f"The loaded symbol file is not a supported ELF: {path}")
        if header[5] not in (1, 2):
            raise ExportError("The ELF has an unsupported byte order")
        digest.update(header)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "host_elf": str(path),
        "elf_sha256": digest.hexdigest(),
        "elf_class_bits": 32 if header[4] == 1 else 64,
        "byte_order": "little" if header[5] == 1 else "big",
    }


def allocator_layout(gdb_api):
    try:
        heap_fields = _fields(gdb_api.lookup_type("struct mm_heap_s"))
        node_type = gdb_api.lookup_type("struct mm_allocnode_s").strip_typedefs()
        node_fields = _fields(node_type)
        word_size = int(gdb_api.lookup_type("mmsize_t").sizeof)
        pointer_bits = int(gdb_api.lookup_type("void").pointer().sizeof) * 8
    except Exception as exc:
        raise ExportError("Default NuttX allocator DWARF types are unavailable") from exc
    required = {"mm_heapstart", "mm_heapend", "mm_nodelist", "mm_delaylist"}
    if not required <= heap_fields or not {"preceding", "size"} <= node_fields:
        raise ExportError("Unsupported allocator layout; only NuttX default mm_heap is supported")
    offsets = {field.name: field.bitpos for field in node_type.fields() if field.name}
    if offsets["preceding"] != 0 or offsets["size"] != word_size * 8:
        raise ExportError("Unsupported default allocator header layout")
    if pointer_bits not in (32, 64) or word_size not in (2, 4, 8):
        raise ExportError("Unsupported target pointer or allocator word size")
    return {
        "header_bytes": int(node_type.sizeof),
        "word_bytes": word_size,
        "node_fields": node_fields,
        "pointer_bits": pointer_bits,
        "address_limit": 1 << pointer_bits,
    }


def discover_heaps(gdb_api, mm, utils, symbols):
    """Use nxgdb's MMHeap reader with a bounded version of its discovery walk."""
    heaps = []
    if symbols:
        for symbol in symbols:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
                raise ExportError("--heap accepts a global symbol name, not a GDB expression")
            value = gdb_api.parse_and_eval(symbol)
            if not value:
                raise ExportError(f"Heap symbol {symbol} is null")
            heaps.append(mm.MMHeap(value, name=symbol))
        discovery = "explicit heap symbols; other heaps may exist"
    else:
        entry = utils.gdb_eval_or_none("g_procfs_meminfo")
        seen = set()
        while entry:
            address = int(entry)
            if address in seen or len(seen) >= MAX_HEAPS:
                raise ExportError("Cyclic or oversized procfs heap registry")
            seen.add(address)
            name = entry["name"].string()
            heaps.append(mm.MMHeap(entry["heap"], name=name))
            entry = entry["next"]
        discovery = "g_procfs_meminfo registry"
        if not heaps:
            value = utils.gdb_eval_or_none("g_mmheap")
            if value:
                heaps.append(mm.MMHeap(value, name="g_mmheap"))
            discovery = "g_mmheap fallback; other heaps may exist (use --heap)"
    if not heaps or len(heaps) > MAX_HEAPS:
        raise ExportError("No supported initialized heap was discovered")
    addresses = [int(heap.address) for heap in heaps]
    if len(set(addresses)) != len(addresses):
        raise ExportError("Duplicate heap registration or --heap symbol")
    return heaps, discovery


def walk_region(start, end, layout, budget):
    """Read chunk extents, including both guard nodes, without payload math."""
    first, last = int(start.address), int(end.address)
    if first >= last:
        raise ExportError("Heap region bounds are invalid or uninitialized")
    region_end = _extent(last, int(end.nodesize), layout["address_limit"])
    nodes = []
    node = start
    expected = first
    previous = None
    while True:
        if len(nodes) >= budget:
            raise ExportError("Heap node limit exceeded; possible corrupt metadata")
        address = int(node.address)
        flags = int(node["size"])
        size = flags & ~3
        if address != expected or size < layout["header_bytes"]:
            raise ExportError(f"Invalid heap chunk at {address:#x}")
        if size % layout["word_bytes"]:
            raise ExportError(f"Unaligned heap chunk size at {address:#x}")
        next_address = _extent(address, size, layout["address_limit"])
        if next_address > region_end or (address < last < next_address):
            raise ExportError(f"Heap chunk crosses the region's guard at {address:#x}")
        is_free = not (flags & 1)
        prev_free = bool(flags & 2)
        if prev_free != bool(previous and previous["free"]):
            raise ExportError(f"Inconsistent previous-free flag at {address:#x}; retry at a safe point")
        if prev_free and int(node["preceding"]) != previous["size"]:
            raise ExportError(f"Inconsistent previous chunk size at {address:#x}")
        if is_free and prev_free:
            raise ExportError("Adjacent free chunks have not coalesced; retry at a safe point")
        guard = address in (first, last)
        if guard and (is_free or size != layout["header_bytes"]):
            raise ExportError(f"Invalid allocator guard node at {address:#x}")
        item = {"start": address, "size": size, "free": is_free, "guard": guard}
        if not guard and not is_free:
            for field in ("pid", "seqno"):
                item[field] = int(node[field]) if field in layout["node_fields"] else None
            item["backtrace"] = []
            if "backtrace" in layout["node_fields"]:
                backtrace = node["backtrace"]
                for i in range(min(_array_length(backtrace), 256)):
                    pc = int(backtrace[i])
                    if not pc:
                        break
                    item["backtrace"].append(pc)
        nodes.append(item)
        if address == last:
            break
        expected = next_address
        if expected > last:
            raise ExportError("Heap traversal did not reach its terminal guard")
        previous = item
        node = node.nextnode
        if node is None:
            raise ExportError("Heap traversal ended before its terminal guard")
    return region_end, nodes


def deferred_nodes(heap, allocated_by_payload, budget):
    """The delayed list occupies released payload; it is allocator metadata."""
    delayed = heap["mm_delaylist"]
    cpus = _array_length(delayed)
    if cpus > MAX_CPUS:
        raise ExportError("Unsupported delayed-free CPU array")
    result = set()
    for cpu in range(cpus):
        entry = delayed[cpu]
        while entry:
            address = int(entry)
            if address in result or len(result) >= budget:
                raise ExportError("Cyclic, duplicate, or oversized delayed-free list")
            if address not in allocated_by_payload:
                raise ExportError("Delayed-free pointer does not match an allocated chunk")
            result.add(address)
            entry = entry["flink"]
    return result


def _span(span_id, name, region, start, size, kind, parent=None, detail=None, source=None, backtrace=None):
    return {
        "id": span_id, "name": name, "region": region, "start": start,
        "size": size, "kind": kind, "source": source, "parent": parent,
        "detail": detail, "backtrace": backtrace or [],
    }


def _overlaps(a, b):
    return a["start"] < b["start"] + b["size"] and b["start"] < a["start"] + a["size"]


def resolve_backtrace(gdb_api, pcs):
    """Resolve each recorded PC through host symbols; retain unresolved frames.

    No live unwind or target function call occurs. A PC is used unchanged:
    architecture-specific return-address adjustments require their own evidence.
    """
    frames = []
    for pc in pcs:
        frame = {"pc": pc, "function": None, "file": None, "line": None}
        try:
            location = gdb_api.find_pc_line(pc)
            if location.symtab and location.symtab.filename:
                frame["file"] = str(location.symtab.filename)
                line = int(getattr(location, "line", 0))
                frame["line"] = line if 0 < line <= 0xffffffff else None
        except Exception:
            pass  # Missing line information must not discard the recorded PC.
        try:
            block = gdb_api.block_for_pc(pc)
            # Walk lexical scopes to the nearest named function, including an
            # inlined function if DWARF identifies one. Do not synthesize frames.
            for _ in range(256):
                if block is None:
                    break
                if block.function is not None:
                    name = block.function.print_name
                    frame["function"] = str(name) if name else None
                    break
                block = block.superblock
        except Exception:
            pass  # Symbol absence is independent of line-table availability.
        frames.append(frame)
    return frames


def collect_heaps(heaps, layout, gdb_api):
    regions, spans, metadata = [], [], []
    node_count = 0
    for heap in heaps:
        heap_address = int(heap.address)
        heap_id = f"heap_{heap_address:x}"
        heap_fields = _fields(heap)
        if not {"mm_heapstart", "mm_heapend", "mm_nodelist", "mm_delaylist"} <= heap_fields:
            raise ExportError(f"Unsupported heap layout at {heap_address:#x}")
        capacity = _array_length(heap["mm_heapstart"])
        count = int(heap.nregions)
        if count < 1 or count > min(capacity, MAX_REGIONS):
            raise ExportError(f"Invalid heap region count at {heap_address:#x}")
        heap_regions = []
        allocated = {}
        for index, (start, end) in enumerate(heap.regions):
            region_id = f"{heap_id}_region_{index}"
            end_address, nodes = walk_region(start, end, layout, MAX_NODES - node_count)
            node_count += len(nodes)
            region = {
                "id": region_id, "name": f"{heap.name} region {index}",
                "start": int(start.address), "size": end_address - int(start.address),
                "kind": "ram", "domain": "cpu_virtual", "physical": None,
            }
            if any(_overlaps(region, other) for other in regions):
                raise ExportError("Overlapping heap regions need an explicit alias or nested-heap adapter")
            regions.append(region)
            heap_regions.append((region, nodes))
            for node in nodes:
                if not node["free"] and not node["guard"]:
                    allocated[node["start"] + layout["header_bytes"]] = node
        pending = deferred_nodes(heap, allocated, MAX_NODES)
        pool_present = "mm_mpool" in heap_fields and bool(heap["mm_mpool"])
        metadata.append({
            "address": heap_address, "name": str(heap.name),
            "pool_slot_occupancy": "not_captured" if pool_present else "no_attached_pool",
            "deferred_free_nodes": len(pending),
        })
        for region, nodes in heap_regions:
            arena_id = region["id"] + "_arena"
            spans.append(_span(arena_id, str(heap.name), region["id"], region["start"],
                               region["size"], "heap_arena", detail="Raw allocator region including guard nodes"))
            for node in nodes:
                address = node["start"]
                kind = "reserved" if node["guard"] else "free" if node["free"] else "allocation"
                name = "Allocator guard" if node["guard"] else "Free chunk" if node["free"] else "Allocated chunk"
                detail = "Raw chunk extent; allocator header and shared boundary metadata included"
                source = None
                backtrace = []
                if kind == "allocation":
                    deferred = address + layout["header_bytes"] in pending
                    if deferred:
                        name = "Deferred free (still allocated)"
                    detail += "; " + json.dumps({
                        "requested_bytes": None, "pid": node["pid"],
                        "allocation_seqno": node["seqno"], "backtrace_pc": node["backtrace"],
                        "deferred_free": deferred,
                    }, separators=(",", ":"))
                    if node["backtrace"]:
                        backtrace = resolve_backtrace(gdb_api, node["backtrace"])
                        source = backtrace[0]["file"]
                        detail += "; source attribution: first recorded backtrace frame; not current semantic owner"
                spans.append(_span(f"{region['id']}_chunk_{address:x}", name, region["id"],
                                   address, node["size"], kind, arena_id, detail, source, backtrace))
    return regions, spans, metadata


def collect_tasks(gdb_api, utils, layout, regions, spans, warnings):
    try:
        count = int(gdb_api.parse_and_eval("g_npidhash"))
    except Exception:
        warnings.append("Task hash unavailable; task stack bounds were not captured")
        return []
    if count < 1 or count > MAX_TASKS:
        raise ExportError("Invalid task hash capacity")
    tasks = []
    seen = set()
    for tcb in utils.get_tcbs():
        address = int(tcb)
        if address in seen:
            raise ExportError("Duplicate TCB in task hash")
        seen.add(address)
        task = {"tcb": address, "span": None, "current_sp": None, "high_water_bytes": None}
        try:
            task.update({
                "pid": int(tcb["pid"]), "name": utils.get_task_name(tcb),
                "stack_alloc_ptr": int(tcb["stack_alloc_ptr"]),
                "stack_base_ptr": int(tcb["stack_base_ptr"]),
                "adj_stack_size": int(tcb["adj_stack_size"]),
            })
            start, size = task["stack_base_ptr"], task["adj_stack_size"]
            end = _extent(start, size, layout["address_limit"])
            if not 0 < task["stack_alloc_ptr"] <= start:
                raise ExportError("Stack allocation pointer is missing or above its usable base")
            extent = {"start": start, "size": size}
            touching = [region for region in regions if _overlaps(extent, region)]
            parent = None
            if touching:
                owners = [span for span in spans if span["kind"] == "allocation"
                          and span["start"] <= start and end <= span["start"] + span["size"]]
                if len(touching) != 1 or len(owners) != 1:
                    raise ExportError("Usable stack is not contained by one raw allocated chunk")
                owner = owners[0]
                region_id, parent = owner["region"], owner["id"]
            else:
                region_id = f"task_{address:x}_stack_region"
                regions.append({"id": region_id, "name": f"Task {task['pid']} usable stack",
                                "start": start, "size": size, "kind": "ram",
                                "domain": "cpu_virtual", "physical": None})
            if any(span["kind"] == "stack" and _overlaps(extent, span) for span in spans):
                raise ExportError("Task usable stacks overlap")
            span_id = f"task_{address:x}_stack"
            spans.append(_span(span_id, f"Task {task['pid']} {task['name']} usable stack",
                               region_id, start, size, "stack", parent,
                               f"TCB {address:#x}; stack_alloc_ptr={task['stack_alloc_ptr']:#x}; "
                               "adjusted usable bounds only; current use and high-water unknown"))
            task["span"] = span_id
        except Exception as exc:
            task["unavailable_reason"] = str(exc)
            warnings.append(f"TCB {address:#x}: no stack span: {exc}")
        tasks.append(task)
    return tasks


def collect_snapshot(gdb_api, mm, utils, label="halted snapshot", time_ns=0, heap_symbols=()):
    if not isinstance(time_ns, int) or not 0 <= time_ns <= U64_MAX:
        raise ExportError("--time-ns must be an unsigned 64-bit integer")
    stopped = require_stopped(gdb_api)
    identity = elf_identity(gdb_api.current_progspace().filename)
    layout = allocator_layout(gdb_api)
    if identity["elf_class_bits"] != layout["pointer_bits"]:
        raise ExportError("Loaded ELF class does not match the target pointer width")
    heaps, discovery = discover_heaps(gdb_api, mm, utils, heap_symbols)
    regions, spans, heap_metadata = collect_heaps(heaps, layout, gdb_api)
    complete = [region["id"] for region in regions]
    warnings = []
    tasks = collect_tasks(gdb_api, utils, layout, regions, spans, warnings)
    if require_stopped(gdb_api) != stopped:
        raise ExportError("The visible target thread inventory changed during capture")
    evidence_warnings = [
        "Snapshot coordinates are supplied manually; no target-time or boot-time clock is inferred.",
        "The ELF hash identifies the loaded host file; running target firmware identity is unverified.",
        "All GDB-reported threads were stopped and chunk topology checked; hidden CPUs and allocator quiescence remain unverified.",
        "Requested allocation sizes, current stack use, and stack high-water are unknown.",
        "Allocation source colors identify the first recorded backtrace frame when host symbols resolve it, not the current semantic owner.",
        "Backtrace frames resolve recorded allocation PCs against host DWARF; unresolved frames are retained and no live stack was unwound.",
        "Pool backing chunks are visible; pool-slot occupancy and physical address aliases are not captured.",
    ]
    return {
        "schema_version": 1,
        "build": {"elf_sha256": identity["elf_sha256"], "label": label},
        "warnings": evidence_warnings + warnings,
        "capture": {
            "exporter": "nxrs-memory-export", "allocator": "nuttx-default",
            **identity, "pointer_bits": layout["pointer_bits"],
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "time_ns_basis": "caller-supplied label coordinate, not a sampled target clock",
            "target_build_identity": "unverified; host ELF hash is not target identity proof",
            "consistency": "all reported threads stopped; chunk topology validated",
            "reported_stopped_threads": stopped,
            "heap_discovery": discovery, "heaps": heap_metadata, "tasks": tasks,
            "warnings": warnings,
            "limits": [
                "Caller must halt every relevant CPU at a quiescent point; GDB may hide cores.",
                "Topology checks do not prove allocator or scheduler transaction quiescence.",
                "No target calls, deferred-free drain, memory payloads, or physical alias inference.",
                "Requested sizes, semantic owners, current stack use, and stack high-water are unknown.",
                "Pool backing chunks are visible; individual pool slots are not captured.",
                "Chunk addresses and optional allocator sequence numbers are not durable lifetime IDs.",
                "This snapshot makes no claim about changes between capture points.",
            ],
        },
        "regions": regions,
        "snapshots": [{"time_ns": time_ns, "label": label, "spans": spans,
                       "complete_regions": complete}],
    }


def write_snapshot(path, snapshot):
    """Do not overwrite a previous capture; serialize fully before creating it."""
    content = json.dumps(snapshot, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    with Path(path).expanduser().open("x", encoding="utf-8") as stream:
        stream.write(content)


def register_command(gdb_api):
    class ExportCommand(gdb_api.Command):
        """nxrs-memory-export OUTPUT [--label TEXT] [--time-ns N] [--heap SYMBOL ...]."""

        def __init__(self):
            super().__init__("nxrs-memory-export", gdb_api.COMMAND_USER)

        def invoke(self, argument, from_tty):
            self.dont_repeat()
            parser = argparse.ArgumentParser(prog="nxrs-memory-export", description=__doc__)
            parser.add_argument("output")
            parser.add_argument("--label", default="halted snapshot")
            parser.add_argument("--time-ns", type=int, default=0)
            parser.add_argument("--heap", action="append", default=[])
            try:
                args = parser.parse_args(gdb_api.string_to_argv(argument))
            except SystemExit:
                return
            try:
                from nxgdb import mm, utils
                snapshot = collect_snapshot(gdb_api, mm, utils, args.label, args.time_ns, args.heap)
                write_snapshot(args.output, snapshot)
            except Exception as exc:
                raise gdb_api.GdbError(f"Memory export failed: {exc}") from exc
            gdb_api.write(f"Wrote {args.output}: {len(snapshot['snapshots'][0]['spans'])} addressed spans.\n")
            gdb_api.write("Host ELF hash recorded; target build identity and allocator quiescence are unverified.\n")
            for warning in snapshot["capture"]["warnings"]:
                gdb_api.write(f"Warning: {warning}\n")

    return ExportCommand()


try:
    import gdb
except ImportError:
    gdb = None

if gdb is not None:
    _command = register_command(gdb)
