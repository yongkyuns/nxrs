"""Stopped side of the same-run memory/timing QEMU witness.

The independent allocator oracle is shared with the existing QEMU test. Native
note decoding below reads the ring bytes directly, independently of the UART
formatter and host trace converter. No target function is called by GDB.
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


def field_bytes(data, typ, name):
    field = next(field for field in typ.fields() if field.name == name)
    require(field.bitpos % 8 == 0, "Unaligned native note field")
    start, size = field.bitpos // 8, field.type.sizeof
    require(start + size <= len(data), "Truncated native note field")
    return int.from_bytes(data[start:start + size], "little")


def last_native_marker(checkpoint_id):
    inferior = gdb.selected_inferior()
    ring = gdb.parse_and_eval("g_noteram_driver")
    drivers = gdb.parse_and_eval("g_note_drivers")
    require(int(drivers[0]) == int(ring.address) and int(drivers[1]) == 0,
            "The stopped witness admits only the fixed RAM note driver")
    require(int(ring["pfd"]) == 0, "An active note poll subscriber could change task state")
    require(int(ring["ni_overwrite"]) == 0, "Native note buffer overflow or overwrite")
    size, head, tail = [int(ring[name]) for name in ("ni_bufsize", "ni_head", "ni_tail")]
    require(size == 32768 and 0 <= head < size and 0 <= tail < size,
            "Unexpected native ring bounds")
    data = bytes(inferior.read_memory(int(ring["ni_buffer"]), size))
    common = gdb.lookup_type("struct note_common_s").strip_typedefs()
    event = gdb.lookup_type("struct note_event_s").strip_typedefs()
    pointer_size = gdb.lookup_type("void").pointer().sizeof
    require(pointer_size == 4, "This native note oracle admits 32-bit AN521 only")
    cursor, consumed, last = tail, 0, None
    available = (head - tail) % size
    while consumed < available:
        length = data[cursor]
        aligned = (length + pointer_size - 1) & ~(pointer_size - 1)
        require(length >= common.sizeof and consumed + aligned <= available,
                "Invalid native note length/alignment")
        last = (data + data)[cursor:cursor + length]
        cursor = (cursor + aligned) % size
        consumed += aligned
    require(last is not None and cursor == head, "Missing final native marker")
    kind = next(field.enumval for field in gdb.lookup_type("enum note_type_e").fields()
                if field.name == "NOTE_DUMP_MARK")
    require(field_bytes(last, common, "nc_type") == kind, "Final native note is not an instant marker")
    # The pinned macro uses sizeof(note_event_s), including structure padding,
    # for the payload length; the actual bytes start at nev_data's offset.
    payload_offset = next(field.bitpos // 8 for field in event.fields() if field.name == "nev_data")
    payload_length = len(last) - event.sizeof
    name = last[payload_offset:payload_offset + payload_length].decode("ascii")
    require(name == f"memory/checkpoint/{checkpoint_id}", "Final native marker ID differs")
    ticks = field_bytes(last, common, "nc_systime")
    require(0 < ticks < 20_000_000 * 5, "Native checkpoint clock is outside the qualified window")
    return {"name": name, "task_id": field_bytes(last, common, "nc_pid"),
            "cpu": field_bytes(last, common, "nc_cpu"), "clock_ticks": ticks,
            "source_time_ns": ticks * 50, "raw_note_hex": last.hex(),
            "ring_head": head, "ring_tail": tail, "ring_size": size}


def checkpoint(settings, checkpoint_id, raw_snapshot, compare_capture):
    output, image = Path(settings["output"]), Path(settings["image"])
    require(all(thread.is_stopped() for inferior in gdb.inferiors() for thread in inferior.threads()),
            "Not every GDB-reported thread is stopped")
    actual_checkpoint = int(gdb.parse_and_eval("g_memory_checkpoint_id"))
    require(actual_checkpoint == checkpoint_id,
            f"Native checkpoint sequence differs: expected {checkpoint_id}, observed {actual_checkpoint}")
    recording_id = gdb.parse_and_eval("g_memory_recording_id").string()
    require(recording_id == settings["recording_id"], "Native recording ID differs")
    require(int(gdb.parse_and_eval("g_memory_checkpoint_primask")) == 1,
            "Target did not establish interrupt exclusion at the marker")
    primask = int(gdb.parse_and_eval("$primask"))
    require(primask == 1, "Interrupts are not masked at the stopped observation")
    require(int(gdb.parse_and_eval("$xpsr")) & 0x1ff == 0,
            "Checkpoint is not in task context")
    running = gdb.parse_and_eval("g_running_tasks[0]")
    basepri = int(gdb.parse_and_eval("$basepri"))
    require(basepri != 0, "Native critical section has not raised BASEPRI")
    # CONFIG_IRQCOUNT is optional in this single-core configuration; its
    # critical-section macro may map directly to up_irq_save(). The actual
    # architectural PRIMASK/BASEPRI registers above are the required proof.
    irqcount = (int(running["irqcount"]) if any(field.name == "irqcount"
                for field in running.type.target().strip_typedefs().fields()) else None)
    require(irqcount is None or irqcount > 0, "Native critical-section nesting is inconsistent")
    marker = last_native_marker(checkpoint_id)
    require(marker["cpu"] == 0 and marker["task_id"] == int(running["pid"]),
            "Marker does not belong to the stopped CPU/task")
    inferior = gdb.selected_inferior()
    timer_before = bytes(inferior.read_memory(0x50001004, 4))
    before = raw_snapshot(image)
    pointers = [int(gdb.parse_and_eval("g_memory_checkpoint_blocks")[index]) for index in range(3)]
    unbound_name = f"unbound-{checkpoint_id}.json"
    command = [str(output / unbound_name), "--label", settings["labels"][checkpoint_id - 1],
               "--time-ns", str(marker["source_time_ns"])]
    gdb.execute("nxrs-memory-export " + shlex.join(command))
    after = raw_snapshot(image)
    require(before == after, "Allocator/task metadata changed during stopped export")
    require(last_native_marker(checkpoint_id) == marker, "Native note ring changed while stopped")
    timer_after = bytes(inferior.read_memory(0x50001004, 4))
    require(timer_before == timer_after, "QEMU peripheral time advanced while GDB stopped the CPUs")
    timer_ticks = 0xffffffff - int.from_bytes(timer_before, "little")
    require(marker["clock_ticks"] <= timer_ticks < 20_000_000 * 5,
            "Stopped timer does not follow the native marker in the admitted epoch")
    capture = json.loads((output / unbound_name).read_text())
    counts = compare_capture(capture, before)
    raw_name = f"raw-{checkpoint_id}.json"
    before["checkpoint"] = {"recording_id": recording_id, "checkpoint_id": checkpoint_id,
                            "marker": marker, "primask": primask, "basepri": basepri,
                            "irqcount": irqcount, "task_id": int(running["pid"]),
                            "block_pointers": pointers, "stopped_timer_ticks": timer_ticks,
                            "timer_unchanged_while_stopped": True}
    (output / raw_name).write_text(json.dumps(before, indent=2) + "\n")
    return {"checkpoint_id": checkpoint_id, "recording_id": recording_id,
            "unbound_file": unbound_name, "raw_file": raw_name,
            "label": settings["labels"][checkpoint_id - 1], "marker": marker,
            "metadata_unchanged": True, "timer_unchanged_while_stopped": True, "counts": counts}


def run():
    settings = json.loads(Path(os.environ["NXRS_MEMORY_LIFECYCLE_RUN"]).read_text())
    output, image = Path(settings["output"]), Path(settings["image"])
    try:
        sys.path.insert(0, settings["test_tools"])
        from qemu_capture_gdb import raw_snapshot, compare_capture, stop_at
        gdb.execute("set pagination off")
        gdb.execute("set confirm off")
        gdb.execute("set non-stop off")
        gdb.execute("target remote | exec " + shlex.join(settings["qemu_command"]))
        # This witness admits one hardware CPU. Step only that thread rather
        # than sending a mixed single-step/continue-all remote packet.
        gdb.execute("set scheduler-locking step")
        stop_at("nsh_prompt")
        sys.path.insert(0, settings["pynuttx"])
        gdb.execute("source " + shlex.quote(str(Path(settings["pynuttx"]) / "gdbinit.py")))
        from nxgdb import mm, utils
        require(Path(mm.__file__).resolve().parent == (Path(settings["pynuttx"]) / "nxgdb").resolve(),
                "Unexpected nxgdb module origin")
        require(callable(utils.get_tcbs), "Missing actual pynuttx task reader")
        gdb.execute("source " + shlex.quote(settings["exporter"]))
        points = []
        # Keep one breakpoint and verify progress beyond the stopped address
        # before rearming it. A repeated remote stop must never be counted as
        # another checkpoint; native marker IDs remain an independent check.
        breakpoint = gdb.Breakpoint("*timing_checkpoint_stop", type=gdb.BP_HARDWARE_BREAKPOINT)
        for checkpoint_id in range(1, 5):
            if checkpoint_id == 1:
                (output / "send-command").write_text("ready\n")
            gdb.execute("continue")
            require(breakpoint.is_valid() and breakpoint.hit_count == checkpoint_id,
                    "Execution stopped before the next checkpoint safe point")
            points.append(checkpoint(settings, checkpoint_id, raw_snapshot, compare_capture))
            if checkpoint_id < 4:
                stopped_pc = int(gdb.newest_frame().read_register("pc"))
                breakpoint.enabled = False
                gdb.execute("stepi")
                require(int(gdb.newest_frame().read_register("pc")) != stopped_pc,
                        "Single-step did not advance beyond the captured checkpoint")
                breakpoint.enabled = True
        breakpoint.delete()
        stop_at("nsh_prompt")
        report = {"schema_version": 1, "evidence": "real_nuttx_qemu_linked_lifecycle",
                  "machine": "mps2-an521", "recording_id": settings["recording_id"],
                  "elf_sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "snapshots": points,
                  "checks": {"independent_raw_header_comparison": True,
                             "independent_native_note_ring": True, "stopped_metadata_unchanged": True,
                             "target_function_calls": False, "timer_unchanged_while_stopped": True}}
        (output / "gdb-report.json").write_text(json.dumps(report, indent=2) + "\n")
        gdb.execute("kill")
        gdb.execute("quit 0")
    except Exception as exc:
        failure = {"error": str(exc), "traceback": traceback.format_exc()}
        (output / "failure.json").write_text(json.dumps(failure, indent=2) + "\n")
        print(failure["traceback"], flush=True)
        gdb.execute("quit 1")


if __name__ == "__main__":
    run()
