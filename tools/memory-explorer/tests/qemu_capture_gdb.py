"""GDB-side real NuttX qualification, invoked by qualify_qemu.py.

The independent reader does not import the exporter or nxgdb heap walkers.
Execution uses continue and temporary hardware breakpoints, never target calls.
"""

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import traceback

import gdb


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fields(typ):
    return {field.name: field for field in typ.strip_typedefs().fields() if field.name}


def read_field(inferior, base, typ, name, byteorder):
    field = fields(typ)[name]
    require(field.bitpos % 8 == 0, f"Non-byte field {name}")
    data = inferior.read_memory(base + field.bitpos // 8, field.type.sizeof)
    return int.from_bytes(bytes(data), byteorder)


def discover_raw_heaps():
    try:
        entry = gdb.parse_and_eval("g_procfs_meminfo")
    except gdb.error:
        entry = None
    heaps, seen = [], set()
    while entry:
        address = int(entry)
        require(address not in seen and len(seen) < 128, "Invalid heap registry")
        seen.add(address)
        heaps.append((entry["heap"], entry["name"].string()))
        entry = entry["next"]
    if not heaps:
        heaps.append((gdb.parse_and_eval("g_mmheap"), "g_mmheap"))
    return heaps


def raw_stack(inferior, tcb, task, byteorder):
    """Read the qualified Cortex-M33 stack ABI without exporter/nxgdb helpers.

    This oracle is intentionally MPS2-specific. Pinned armv8-m/irq.h stores
    REG_R13 at word zero of xcp.regs; ARM arm_checkstack.c scans eight-byte
    aligned bounds in 32-bit STACK_COLOR words. Preserve the actual bytes so
    the Rust gate can independently calculate the watermark again.
    """
    base, size = task["stack_base_ptr"], task["adj_stack_size"]
    if not size:
        return None
    require(base > 0 and 0 < size <= 1024 * 1024, "Invalid raw stack extent")
    frame = gdb.newest_frame()
    xpsr = int(frame.read_register("xpsr"))
    running = int(gdb.parse_and_eval("g_running_tasks[0]"))
    source = "live_register" if task["tcb"] == running else "saved_context"
    record = {"base": base, "size": size, "running_tcb": running,
              "xpsr": xpsr, "sp_source": source}
    if source == "live_register":
        require(xpsr & 0x1ff == 0, "Running stack oracle requires task context")
        record["sp"] = int(frame.read_register("sp"))
    else:
        registers = int(tcb["xcp"]["regs"])
        require(registers != 0, "Nonrunning raw task lacks saved context")
        raw_sp = bytes(inferior.read_memory(registers, 4))
        record.update({"saved_context_address": registers,
                       "saved_sp_word_hex": raw_sp.hex(),
                       "sp": int.from_bytes(raw_sp, byteorder)})
    require(base <= record["sp"] <= base + size, "Raw task SP exceeds its reservation")
    start, end = (base + 7) & ~7, (base + size) & ~7
    require(start < end, "Raw aligned stack interval is empty")
    data = bytes(inferior.read_memory(start, end - start))
    record.update({"scan_start": start, "scan_size": len(data),
                   "bytes_hex": data.hex(), "bytes_sha256": hashlib.sha256(data).hexdigest(),
                   "byteorder": byteorder, "pattern": 0xdeadbeef, "word_bytes": 4})
    first_changed = next((offset for offset in range(0, len(data), 4)
                          if int.from_bytes(data[offset:offset + 4], byteorder) != 0xdeadbeef),
                         len(data))
    record["used_bytes"] = len(data) - first_changed
    return record


def raw_snapshot(image, include_stack_bytes=False):
    data = image.read_bytes()
    require(data[:4] == b"\x7fELF", "Expected an ELF")
    byteorder = "little" if data[5] == 1 else "big"
    inferior = gdb.selected_inferior()
    alloc_type = gdb.lookup_type("struct mm_allocnode_s")
    alloc_size = int(alloc_type.sizeof)
    result = {"elf_sha256": hashlib.sha256(data).hexdigest(), "heaps": [], "tasks": []}
    total_nodes = 0
    for heap_value, name in discover_raw_heaps():
        heap = heap_value.dereference() if heap_value.type.code == gdb.TYPE_CODE_PTR else heap_value
        count = int(heap["mm_nregions"]) if "mm_nregions" in fields(heap.type) else 1
        require(0 < count <= 128, "Invalid raw region count")
        raw_heap = {"address": int(heap.address), "name": name, "regions": [], "deferred_payloads": []}
        allocated_payloads = set()
        for index in range(count):
            first = int(heap["mm_heapstart"][index])
            last = int(heap["mm_heapend"][index])
            require(0 < first < last, "Invalid raw heap bounds")
            cursor = first
            chunks = []
            previous_free, previous_size = False, 0
            while cursor <= last:
                total_nodes += 1
                require(total_nodes < 262144, "Raw heap traversal exceeded bound")
                flags = read_field(inferior, cursor, alloc_type, "size", byteorder)
                size = flags & ~3
                free = (flags & 1) == 0
                guard = cursor in (first, last)
                require(size >= alloc_size, "Invalid raw node size")
                require(bool(flags & 2) == previous_free, "Invalid raw previous-free flag")
                if previous_free:
                    require(read_field(inferior, cursor, alloc_type, "preceding", byteorder) == previous_size,
                            "Invalid raw preceding size")
                if guard:
                    require(not free and size == alloc_size, "Invalid raw guard")
                else:
                    require(cursor + size <= last, "Raw chunk crosses terminal guard")
                kind = "reserved" if guard else "free" if free else "allocation"
                chunks.append({"start": cursor, "size": size, "kind": kind})
                if kind == "allocation":
                    allocated_payloads.add(cursor + alloc_size)
                if cursor == last:
                    break
                require(not (free and previous_free), "Adjacent uncoalesced raw free nodes")
                previous_free, previous_size = free, size
                cursor += size
            require(cursor == last, "Raw walk missed terminal guard")
            raw_heap["regions"].append({"start": first, "size": last + alloc_size - first, "chunks": chunks})
        delayed = heap["mm_delaylist"]
        low, high = delayed.type.strip_typedefs().range()
        require(low == 0 and high < 256, "Invalid raw delayed-free array")
        seen = set()
        for cpu in range(high + 1):
            entry = delayed[cpu]
            while entry:
                address = int(entry)
                require(address not in seen and address in allocated_payloads, "Invalid raw deferred free")
                seen.add(address)
                raw_heap["deferred_payloads"].append(address)
                entry = entry["flink"]
        raw_heap["deferred_payloads"].sort()
        result["heaps"].append(raw_heap)
    count = int(gdb.parse_and_eval("g_npidhash"))
    require(0 < count < 65536, "Invalid raw task hash size")
    table = gdb.parse_and_eval("g_pidhash")
    for index in range(count):
        tcb = table[index]
        if tcb:
            task = {"tcb": int(tcb), **{
                name: int(tcb[name]) for name in ("pid", "stack_alloc_ptr", "stack_base_ptr", "adj_stack_size")
            }}
            if include_stack_bytes:
                task["stack_bytes"] = raw_stack(inferior, tcb, task, byteorder)
            result["tasks"].append(task)
    result["tasks"].sort(key=lambda task: task["tcb"])
    return result


def compare_capture(capture, raw):
    require(capture["build"]["elf_sha256"] == raw["elf_sha256"], "Exporter ELF hash differs")
    spans = capture["snapshots"][0]["spans"]
    arenas = [span for span in spans if span["kind"] == "heap_arena"]
    expected_regions = [region for heap in raw["heaps"] for region in heap["regions"]]
    require(len(arenas) == len(expected_regions), "Exporter/raw region count differs")
    counts = {"heaps": len(raw["heaps"]), "regions": len(expected_regions), "chunks": 0,
              "allocation_count": 0, "free_count": 0, "reserved_count": 0,
              "allocated_chunk_bytes": 0, "free_chunk_bytes": 0, "reserved_bytes": 0,
              "task_count": len(raw["tasks"]), "stack_count": 0, "source_attributed_count": 0,
              "live_stack_pointers": 0, "saved_stack_pointers": 0, "colored_stack_scans": 0}
    for region in expected_regions:
        matching = [arena for arena in arenas if (arena["start"], arena["size"]) == (region["start"], region["size"])]
        require(len(matching) == 1, "Missing or duplicate exported arena")
        arena = matching[0]
        observed = sorted(({key: span[key] for key in ("start", "size", "kind")}
                           for span in spans if span["parent"] == arena["id"]), key=lambda chunk: chunk["start"])
        require(observed == region["chunks"], "Exporter chunks differ from direct raw header bytes")
        for chunk in observed:
            counts["chunks"] += 1
            counts[chunk["kind"] + "_count"] += 1
            byte_key = {"allocation": "allocated_chunk_bytes", "free": "free_chunk_bytes", "reserved": "reserved_bytes"}[chunk["kind"]]
            counts[byte_key] += chunk["size"]
    direct_tasks = {task["tcb"]: task for task in raw["tasks"]}
    tasks = capture["capture"]["tasks"]
    require(len(tasks) == len(direct_tasks), "Exporter task inventory differs")
    require({task["tcb"] for task in tasks} == set(direct_tasks), "Exporter task inventory is not a bijection")
    for task in tasks:
        require(task["tcb"] in direct_tasks, "Exporter has an unknown TCB")
        expected = direct_tasks[task["tcb"]]
        require(all(task[key] == expected[key] for key in
                    ("tcb", "pid", "stack_alloc_ptr", "stack_base_ptr", "adj_stack_size")),
                "Exporter task fields differ")
        if task["span"]:
            stack = next(span for span in spans if span["id"] == task["span"])
            require((stack["start"], stack["size"]) == (expected["stack_base_ptr"], expected["adj_stack_size"]),
                    "Exporter stack interval differs from raw TCB bounds")
            counts["stack_count"] += 1
    observations = capture["snapshots"][0]["stack_observations"]
    require(len(observations) == counts["stack_count"], "Missing task stack observations")
    require(len({item["tcb"] for item in observations}) == len(observations),
            "Duplicate task stack observations")
    for observation in observations:
        task = direct_tasks[observation["tcb"]]
        require(observation["task_id"] == task["pid"] and observation["growth"] == "down",
                "Task stack observation identity or growth differs")
        inventory = next(item for item in tasks if item["tcb"] == task["tcb"])
        require(observation["stack"] == inventory["span"], "Observation references another task stack")
        raw_stack_bytes = task["stack_bytes"]
        require(raw_stack_bytes is not None, "Observation has no independently read stack")
        current = observation["current"]
        if current["status"] == "observed":
            require(current["sp"] == raw_stack_bytes["sp"] and
                    current["source"] == raw_stack_bytes["sp_source"],
                    "Exporter stack pointer differs from live register/saved raw context")
            counts["live_stack_pointers" if current["source"] == "live_register" else
                   "saved_stack_pointers"] += 1
        else:
            require(current["status"] == "unavailable" and current["reason"].strip(),
                    "Unavailable current depth lacks an explicit reason")
        high_water = observation["high_water"]
        if high_water["status"] == "coloration":
            require(all(high_water[key] == raw_stack_bytes[key] for key in
                        ("scan_start", "scan_size", "used_bytes", "pattern", "word_bytes")),
                    "Exporter stack watermark differs from independent stack bytes")
            counts["colored_stack_scans"] += 1
        else:
            require(high_water["status"] == "unavailable" and high_water["reason"].strip(),
                    "Unavailable high-water observation lacks an explicit reason")
    counts["source_attributed_count"] = sum(span["kind"] == "allocation" and bool(span["source"]) for span in spans)
    require(counts["allocation_count"] > 0 and counts["free_count"] > 0, "Expected live allocations and free space")
    require(counts["stack_count"] > 0, "Expected at least one verified task stack")
    return counts


def checkpoint(output, image, index, label):
    before = raw_snapshot(image, include_stack_bytes=True)
    capture_file, raw_file = f"capture-{index}.json", f"raw-{index}.json"
    time_ns = index * 1_000_000_000
    command = [str(output / capture_file), "--label", label, "--time-ns", str(time_ns)]
    gdb.execute("nxrs-memory-export " + shlex.join(command))
    after = raw_snapshot(image, include_stack_bytes=True)
    require(before == after, "Allocator/task metadata changed during the stopped export")
    capture = json.loads((output / capture_file).read_text())
    counts = compare_capture(capture, before)
    (output / raw_file).write_text(json.dumps(before, indent=2) + "\n")
    summary = {"capture_file": capture_file, "raw_file": raw_file, "label": label, "time_ns": time_ns,
               "metadata_unchanged": True, "counts": counts}
    print("NXRS_QEMU_CAPTURE " + json.dumps(summary), flush=True)
    return summary


def stop_at(symbol):
    breakpoint = gdb.Breakpoint(symbol, type=gdb.BP_HARDWARE_BREAKPOINT, temporary=True)
    gdb.execute("continue")
    require(not breakpoint.is_valid(), f"Execution stopped before reaching {symbol}")
    require(all(thread.is_stopped() for inferior in gdb.inferiors() for thread in inferior.threads()),
            "Not every reported thread is stopped")


def terminate_remote():
    """Finish a validated witness; the host still owns final killpg/wait.

    QEMU exits when GDB sends the kill packet. GDB 16 can then diagnose its
    expected pipe EOF as an exception instead of acknowledging the kill.
    Record only that exact teardown case; all other debugger errors fail.
    Disconnect is unsuitable here because GDB waits for its still-live pipe
    child, which cannot reach the host's cleanup until the deadline expires.
    """
    try:
        gdb.execute("kill")
    except gdb.error as error:
        detail = str(error)
        if "Target disconnected" not in detail or "Broken pipe" not in detail:
            raise
        return {"kill_packet": "remote_eof", "detail": detail}
    return {"kill_packet": "acknowledged"}


def run():
    settings = json.loads(Path(os.environ["NXRS_MEMORY_QEMU_RUN"]).read_text())
    output, image = Path(settings["output"]), Path(settings["image"])
    try:
        gdb.execute("set pagination off")
        gdb.execute("set confirm off")
        gdb.execute("set non-stop off")
        gdb.execute("target remote | exec " + shlex.join(settings["qemu_command"]))
        stop_at("nsh_prompt")
        # Nested GDB source execution may retain the outer script's __file__;
        # make the exact requested upstream package importable explicitly.
        sys.path.insert(0, str(Path(settings["pynuttx"]).resolve()))
        gdb.execute("source " + shlex.quote(str(Path(settings["pynuttx"]) / "gdbinit.py")))
        # Optional pynuttx commands may lack dependencies, but actual mm/utils
        # must import successfully and come from the requested pinned source.
        from nxgdb import mm, utils
        require(Path(mm.__file__).resolve().parent == (Path(settings["pynuttx"]) / "nxgdb").resolve(),
                "Unexpected nxgdb module origin")
        require(callable(utils.get_tcbs), "Missing actual pynuttx task reader")
        gdb.execute("source " + shlex.quote(settings["exporter"]))
        snapshots = [checkpoint(output, image, 0, "nsh_ready")]
        if settings["command"]:
            entry = gdb.Breakpoint("std_demo_main", type=gdb.BP_HARDWARE_BREAKPOINT, temporary=True)
            (output / "send-command").write_text("ready\n")
            gdb.execute("continue")
            require(not entry.is_valid(), "Execution stopped before std_demo_main")
            snapshots.append(checkpoint(output, image, 1, "std_demo_entry"))
            stop_at("nsh_prompt")
            snapshots.append(checkpoint(output, image, 2, "after_std_demo"))
        report = {"schema_version": 1, "evidence": "real_nuttx_qemu", "machine": "mps2-an521",
                  "elf_sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "snapshots": snapshots,
                  "checks": {"independent_raw_header_comparison": True, "stopped_metadata_unchanged": True,
                             "target_function_calls": False},
                  "limits": ["QEMU Cortex-M33 runtime proxy, not physical-board qualification",
                             "Coordinates are ordered observations, not measured elapsed time",
                             "The host launched this exact ELF; no separate device attestation is claimed"]}
        report["remote_teardown"] = terminate_remote()
        (output / "gdb-report.json").write_text(json.dumps(report, indent=2) + "\n")
        gdb.execute("quit 0")
    except Exception as exc:
        failure = {"error": str(exc), "traceback": traceback.format_exc()}
        (output / "failure.json").write_text(json.dumps(failure, indent=2) + "\n")
        print(failure["traceback"], flush=True)
        gdb.execute("quit 1")


if __name__ == "__main__":
    run()
