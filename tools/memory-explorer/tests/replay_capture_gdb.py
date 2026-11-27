"""Read-only GDB observer for the isolated heap replay fixture.

The regular probe walker retains every native allocation header. A separately
implemented existing oracle reads complete heaps at application safe points.
No target functions, memory writes, or fabricated target timestamps are used.
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


def header_layout():
    typ = gdb.lookup_type("struct mm_allocnode_s").strip_typedefs()
    result = {"size": int(typ.sizeof), "fields": {}, "allocated_mask": 1,
              "previous_free_mask": 2, "byte_order": "little"}
    for field in typ.fields():
        if field.name in ("size", "preceding"):
            require(field.bitpos % 8 == 0, "Non-byte native allocator header field")
            result["fields"][field.name] = {"offset": field.bitpos // 8, "size": int(field.type.sizeof)}
    require(set(result["fields"]) == {"size", "preceding"}, "Unexpected allocator header ABI")
    return result


def raw_heap(heap, layout):
    inferior = gdb.selected_inferior()
    fields = {field.name for field in heap.type.target().strip_typedefs().fields()}
    require("mm_nregions" not in fields or int(heap["mm_nregions"]) == 1,
            "The isolated heap changed its region count")
    first, last = int(heap["mm_heapstart"][0]), int(heap["mm_heapend"][0])
    arena = int(gdb.parse_and_eval("&g_replay_arena"))
    require(arena < first < last < arena + 65536, "Heap bounds leave the fixed fixture arena")
    cursor, chunks, previous_free, previous_size = first, [], False, 0
    allocated_payloads = set()
    while True:
        require(len(chunks) < 256, "Isolated heap exceeds 256 chunks")
        raw = bytes(inferior.read_memory(cursor, layout["size"]))

        def value(name):
            field = layout["fields"][name]
            return int.from_bytes(raw[field["offset"]:field["offset"] + field["size"]], "little")

        flags = value("size")
        size = flags & ~3
        free = flags & 1 == 0
        guard = cursor in (first, last)
        require(size >= layout["size"] and bool(flags & 2) == previous_free,
                "Invalid chunk size or previous-free flag")
        if previous_free:
            require(value("preceding") == previous_size, "Invalid previous-free chunk extent")
        if guard:
            require(not free and size == layout["size"], "Invalid allocator guard")
        else:
            require(cursor + size <= last and not (free and previous_free), "Uncommitted allocator geometry")
        state = "reserved" if guard else "free" if free else "allocated"
        chunks.append({"start": cursor, "size": size, "state": state, "header_hex": raw.hex()})
        if state == "allocated":
            allocated_payloads.add(cursor + layout["size"])
        if cursor == last:
            break
        require(cursor + size <= last, "Chunk walk passed the terminal guard")
        previous_free, previous_size = free, size
        cursor += size
    deferred = []
    entry = heap["mm_delaylist"][0]
    while entry:
        address = int(entry)
        require(address not in deferred and address in allocated_payloads and len(deferred) < 256,
                "Invalid delayed-free chain")
        deferred.append(address)
        entry = entry["flink"]
    return {"region": {"start": first, "size": last + layout["size"] - first},
            "chunks": chunks, "deferred_payloads": sorted(deferred),
            "arena_start": arena, "arena_bytes": 65536, "metadata_prefix_bytes": first - arena}


def observe(settings, layout, independent_reader):
    require(all(thread.is_stopped() for inferior in gdb.inferiors() for thread in inferior.threads()),
            "Not every GDB thread is stopped")
    kind_id = int(gdb.parse_and_eval("g_replay_kind"))
    kind = {1: "initial", 2: "begin", 3: "commit", 4: "safe", 5: "final"}.get(kind_id)
    require(kind is not None, "Unexpected target probe kind")
    require(gdb.parse_and_eval("g_replay_recording_id").string() == settings["recording_id"],
            "Target recording/reset identity differs")
    faults = int(gdb.parse_and_eval("g_replay_fault"))
    require(faults == 0, "Target fixture recorded an invariant failure")
    primask, basepri, xpsr = [int(gdb.parse_and_eval("$" + name)) for name in ("primask", "basepri", "xpsr")]
    require(primask == 1 and int(gdb.parse_and_eval("g_replay_primask")) == 1 and basepri != 0,
            "Probe did not exclude interrupts")
    require(xpsr & 0x1ff == 0, "Probe is outside admitted task context")
    task_id = int(gdb.parse_and_eval("g_running_tasks[0]->pid"))
    require(task_id == int(gdb.parse_and_eval("g_replay_owner")), "Heap probe belongs to another task")
    heap = gdb.parse_and_eval("g_replay_heap")
    holder = int(heap["mm_lock"]["sem"]["val"]["mholder"])
    if kind in ("begin", "commit"):
        # mm_lock's return code alone does not establish ownership in the pinned
        # implementation. Validate the actual native mutex field independently.
        require(holder & 0x7fffffff == task_id, "Fixture task does not actually hold the heap mutex")
    else:
        require(holder == 0x7ffffffe, "Application safe point still owns an allocator lock")
    inferior = gdb.selected_inferior()
    timer_before = bytes(inferior.read_memory(0x50001004, 4))
    ticks = int(gdb.parse_and_eval("g_replay_ticks"))
    require(0 < ticks < 20_000_000 * 5, "Target time leaves the qualified timer epoch")
    before = raw_heap(heap, layout)
    event = {"kind": kind, "sequence": int(gdb.parse_and_eval("g_replay_sequence")),
             "probe_sequence": int(gdb.parse_and_eval("g_replay_probe_sequence")),
             "operation": int(gdb.parse_and_eval("g_replay_operation")), "ticks": ticks, "time_ns": ticks * 50,
             "task_id": task_id, "heap_address": int(heap), "primask": primask, "basepri": basepri,
             "xpsr": xpsr, "faults": faults,
             "mutex_holder": holder, "begin_count": int(gdb.parse_and_eval("g_replay_begin_count")),
             "commit_count": int(gdb.parse_and_eval("g_replay_commit_count")),
             "safe_count": int(gdb.parse_and_eval("g_replay_safe_count")),
             "blocks": [int(gdb.parse_and_eval("g_replay_blocks")[index]) for index in range(4)], **before}
    if kind in ("initial", "safe", "final"):
        independent = independent_reader(Path(settings["image"]))
        matching = [item for item in independent["heaps"] if item["address"] == int(heap)]
        require(len(matching) == 1, "Independent heap registry did not identify the isolated heap")
        event["independent_safe_heap"] = matching[0]
    after = raw_heap(heap, layout)
    require(before == after, "Raw allocator state changed while stopped")
    timer_after = bytes(inferior.read_memory(0x50001004, 4))
    require(timer_before == timer_after, "QEMU peripheral time advanced during GDB reads")
    event["timer_ticks"] = 0xffffffff - int.from_bytes(timer_before, "little")
    require(event["timer_ticks"] >= ticks, "Native probe timestamp is after the stopped clock")
    event["timer_unchanged"] = True
    event["metadata_unchanged"] = True
    return event


def run():
    settings = json.loads(Path(os.environ["NXRS_HEAP_REPLAY_RUN"]).read_text())
    output = Path(settings["output"])
    try:
        sys.path.insert(0, settings["test_tools"])
        from qemu_capture_gdb import raw_snapshot, stop_at
        gdb.execute("set pagination off")
        gdb.execute("set confirm off")
        gdb.execute("set non-stop off")
        gdb.execute("target remote | exec " + shlex.join(settings["qemu_command"]))
        stop_at("nsh_prompt")
        layout = header_layout()
        breakpoint = gdb.Breakpoint("*replay_probe_stop", type=gdb.BP_HARDWARE_BREAKPOINT)
        (output / "send-command").write_text("ready\n")
        events = []
        while len(events) < 2 * 256 + 26:
            gdb.execute("continue")
            require(int(gdb.parse_and_eval("$pc")) == int(gdb.parse_and_eval("&replay_probe_stop")) & ~1,
                    "Execution stopped outside the retained probe")
            events.append(observe(settings, layout, raw_snapshot))
            if events[-1]["kind"] == "final":
                break
            # QEMU can report the same persistent hardware breakpoint again
            # while GDB resumes through its entry. Advance one real instruction
            # with the breakpoint disabled; never deduplicate native records.
            breakpoint.enabled = False
            for step_attempt in range(8):
                gdb.execute("stepi")
                require(int(gdb.parse_and_eval("g_replay_probe_sequence")) == events[-1]["probe_sequence"],
                        "Single stepping crossed another native probe")
                if int(gdb.parse_and_eval("$pc")) != int(gdb.parse_and_eval("&replay_probe_stop")) & ~1:
                    break
            else:
                raise RuntimeError("Single stepping did not advance past the probe entry")
            events[-1]["resume_step_attempts"] = step_attempt + 1
            breakpoint.enabled = True
        else:
            raise RuntimeError("Too many target probe stops")
        breakpoint.delete()
        stop_at("nsh_prompt")
        report = {"schema_version": 1, "evidence": "real_nuttx_qemu_isolated_heap_replay",
                  "elf_sha256": hashlib.sha256(Path(settings["image"]).read_bytes()).hexdigest(),
                  "recording_id": settings["recording_id"], "header_layout": layout, "events": events,
                  "target_function_calls": False}
        (output / "raw-events.json").write_text(json.dumps(report, indent=2) + "\n")
        # A kill packet may race the QEMU pipe shutdown. This cleanup result
        # does not contribute evidence: the host still requires the complete
        # native footer and every retained observation, and owns the process
        # group even if the debugger cannot acknowledge emulator termination.
        try:
            gdb.execute("kill")
        except gdb.error as exc:
            (output / "teardown.json").write_text(json.dumps({"kill_response": str(exc)}) + "\n")
        gdb.execute("quit 0")
    except Exception as exc:
        failure = {"error": str(exc), "traceback": traceback.format_exc()}
        (output / "failure.json").write_text(json.dumps(failure, indent=2) + "\n")
        print(failure["traceback"], flush=True)
        gdb.execute("quit 1")


if __name__ == "__main__":
    run()
