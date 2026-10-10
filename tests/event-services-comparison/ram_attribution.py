#!/usr/bin/env python3
"""Attribute a measured full/lean report using its exact, frozen ELF images.

This is a reservation ledger, not a prediction of a stripped production image.
Only identified objects are classified; remaining storage stays explicitly mixed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.images import parse_sections, section_accounting, esp_idf_section_accounting


def objects(output, sections, resident_names):
    """Ignore linker markers, absolute aliases and objects outside counted RAM."""
    ranges = [(s["address"], s["address"] + s["size"]) for s in sections
              if s["name"] in resident_names and "X" not in s["flags"]]
    result = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[3] != "OBJECT" or not fields[6].isdigit():
            continue
        address, size, name = int(fields[1], 16), int(fields[2]), fields[7]
        if size and any(lo <= address and address + size <= hi for lo, hi in ranges):
            result.append((address, size, name))
    return result


class Ledger:
    def __init__(self, symbols):
        self.symbols = symbols
        self.used = []
        self.evidence = {}

    def take(self, *names):
        """Select each object once; reject missing, ambiguous or overlapping names."""
        total = 0
        for name in names:
            matches = [s for s in self.symbols if s[2] == name or
                       re.search(r"(?:board|service|clock)\d+" + re.escape(name) + r"17h", s[2])]
            if len(matches) != 1:
                raise ValueError(f"expected one resident object: {name}")
            address, size, symbol = matches[0]
            if any(address < a + n and a < address + size for a, n in self.used):
                raise ValueError(f"overlapping/reused resident object: {name}")
            self.used.append((address, size))
            self.evidence[symbol] = size
            total += size
        return total


def attribute(case, sections, symbols, config=""):
    image = case["flash_and_ram"]
    accounting = (esp_idf_section_accounting if case["platform"] == "embassy"
                  else section_accounting)(sections)
    if accounting["resident_ram_bytes"] != image["resident_static_ram_bytes"]:
        raise ValueError("ELF resident sections differ from the measured report")
    normal = case["profiles"]["normal"]
    resource = lambda name: normal["resource_metrics"][name]["max"]
    state_bytes = normal["memory_metrics"]["application_state"]["max"]
    check_bytes = sum(normal["memory_metrics"][name]["max"] for name in
                      ("diagnostics", "scheduling_diagnostics"))
    check_bytes += normal["control_metrics"]["diagnostic_bytes"]["max"]
    events = resource("queue_buffers")
    if events != case["configuration"]["slots"] * case["configuration"]["event_bytes"]:
        raise ValueError("reported queue capacity differs from the event schema")
    ledger = Ledger(symbols)
    code = sum(s["size"] for s in sections if "X" in s["flags"] and
               s["name"] in accounting["resident_ram_sections"])
    parts = dict(ram_code=code, platform_stacks_and_arena=0,
                 execution_workspace=resource("stack_storage"), event_capacity=events,
                 queue_adapter_controls=0, fixture_application_state=state_bytes,
                 known_test_storage=0,
                 linked_data_not_split=0, live_heap_not_split=0, heap_peak_margin=0)
    details = {}
    # Only storage physically present in the ELF is removed from its remainder.
    static_execution, static_events = parts["execution_workspace"], events
    if case["platform"] == "embassy":
        containers = ledger.take("RESOURCES", "EXTRA", "SCHED_EXTRA")
        if containers < state_bytes + check_bytes:
            raise ValueError("checker containers cannot hold reported checking state")
        # RESOURCES contains both fixture application state and diagnostics.
        # Assign its state fields separately; container padding remains overhead.
        parts["known_test_storage"] = containers - state_bytes + ledger.take(
            "GATES", "DEADLINES", "READY", "DONE", "EPOCH", "PROFILE", "PEAK",
            "RUNNING", "CLOCK_DONE", "CLOCK_TICKS", "CLOCK_START", "PENDING")
        # The clock pool is a synthetic-release coordinator, not a service task.
        clock = [s[2] for s in symbols if re.search(r"5clock4POOL17h", s[2])]
        services = [s[2] for s in symbols if re.search(r"7service4POOL17h", s[2])]
        if len(clock) != 1 or len(services) != 1:
            raise ValueError("expected clock and service future pools")
        parts["known_test_storage"] += ledger.take(clock[0])
        futures = ledger.take(services[0])
        parts["execution_workspace"] += futures
        static_execution += futures
        parts["queue_adapter_controls"] = ledger.take("INBOXES") - events + ledger.take("EXECUTOR")
        details.update(service_future_pool=futures,
                       checker_container_padding=containers-state_bytes-check_bytes)
    else:
        if ledger.take("states") != state_bytes:
            raise ValueError("application-state objects differ from reported fixture state")
        checking = ledger.take("diagnostics", "scheduling_extra", "work_duration",
                               "work_jobs", "hal_calls", "hal_errors")
        if checking != check_bytes:
            raise ValueError("checker object sizes differ from reported checking state")
        gates = ["epoch", "profile", "queue_peak", "ready_gate", "go_gate",
                 "ready_consumed", "released", "spawn_failed"]
        native = case["platform"].startswith("nuttx")
        if native:
            gates += ["gates_ready", "invocation_id"]
        parts["known_test_storage"] = checking + ledger.take(*gates)
        adapter = ledger.take("roles", "threads", "thread_joined", "ready_cursor",
                              *(('queue_handles', 'queue_names', 'queue_opened', 'poll_sets')
                                if native else ('queues', 'poll_events')))
        expected = sum(resource(n) for n in ("queue_objects", "thread_objects", "entry_storage"))
        if adapter != expected:
            raise ValueError("adapter ELF objects differ from reported resource sizes")
        parts["queue_adapter_controls"] = adapter
        details["adapter_objects"] = adapter
        if native:
            static_execution = 0
            parts["platform_stacks_and_arena"] = ledger.take("g_idlestack", "g_intstackalloc")
            pool = ledger.take("g_msgpool")
            values = dict(re.findall(r"^CONFIG_(\w+)=(-?\d+)$", config, re.M))
            required = {"MQ_MAXMSGSIZE": "248", "MM_DEFAULT_ALIGNMENT": "8",
                        "MM_NODE_GUARDSIZE": "0", "MM_BACKTRACE": "-1"}
            if any(values.get(k) != v for k, v in required.items()) or "CONFIG_MM_SMALL=y" in config:
                raise ValueError("unsupported NuttX message/allocator layout")
            prealloc = int(values["PREALLOC_MQ_MSGS"])
            slots, event_bytes = case["configuration"]["slots"], case["configuration"]["event_bytes"]
            if not 0 <= prealloc <= slots:
                raise ValueError("invalid general message pool count")
            # 32-bit mqueue_msg_s is 12 bytes (mail[1] included). The allocator
            # charges 4 header bytes: its preceding field overlaps the prior node.
            node_bytes = ((event_bytes + 11 + 4 + 7) // 8) * 8
            cycles = [cycle for run in case["profiles"]["saturation"]["runs"] for cycle in run["cycles"]]
            if not cycles or any(c["full_heap"] - c["empty_heap"] !=
                                 (slots - prealloc) * node_bytes for c in cycles):
                raise ValueError("full/empty heap change does not match message allocation model")
            if len({c["empty_heap"] for c in cycles}) != 1 or any(
                    c["drained_heap"] != c["empty_heap"] for c in cycles):
                raise ValueError("capacity heap snapshots are not stable/reversible")
            static_events = prealloc * event_bytes
            spare = pool - static_events
            message_overhead = (slots - prealloc) * (node_bytes - event_bytes)
            parts["queue_adapter_controls"] += spare + message_overhead
            parts["live_heap_not_split"] = cycles[0]["empty_heap"] - parts["execution_workspace"]
            parts["heap_peak_margin"] = image["nfree_maxused_peak_bytes"] - max(c["full_heap"] for c in cycles)
            details.update(fixed_message_pool=pool, pool_event_contents=static_events,
                           pool_spare_and_headers=spare, dynamic_message_overhead=message_overhead,
                           dynamic_message_contents=events-static_events, allocated_message_bytes=node_bytes)
        else:
            if ledger.take("worker_stacks") != static_execution or ledger.take("queue_buffers") != events:
                raise ValueError("static stack/event reservations differ from report")
            parts["platform_stacks_and_arena"] = ledger.take(
                "z_main_stack", "z_interrupt_stacks", "z_idle_stacks", "sys_work_q_stack", "kheap__system_heap")
    static_controls = parts["queue_adapter_controls"] - details.get("dynamic_message_overhead", 0)
    parts["linked_data_not_split"] = image["resident_static_ram_bytes"] - sum((
        code, parts["platform_stacks_and_arena"], static_execution, static_events,
        static_controls, state_bytes, parts["known_test_storage"]))
    if min(parts.values()) < 0 or sum(parts.values()) != image["whole_ram_peak_bytes"]:
        raise ValueError("RAM attribution does not reconcile to the measured total")
    return dict(elf_sha256=case["elf_sha256"], components=parts, details=details,
                object_bytes=ledger.evidence, whole_ram_peak_bytes=sum(parts.values()))


def frozen_attribution(case, directory, readelf):
    elf = directory / "app.elf"
    provenance = json.loads((directory / "build-provenance.json").read_text())
    digest = hashlib.sha256(elf.read_bytes()).hexdigest()
    if digest != case["elf_sha256"] or digest != provenance["artifacts"]["app.elf"]:
        raise ValueError("frozen ELF identity differs from the measured report")
    output = subprocess.check_output([readelf, "-W", "-S", "-s", str(elf)], text=True)
    sections = parse_sections(output)
    if sections != provenance["elf_sections"]:
        raise ValueError("frozen ELF sections differ from provenance")
    config = ""
    if case["platform"].startswith("nuttx"):
        path = directory / "resolved.config"
        if hashlib.sha256(path.read_bytes()).hexdigest() != provenance["artifacts"]["resolved.config"]:
            raise ValueError("frozen NuttX configuration identity differs")
        config = path.read_text()
    return attribute(case, sections, objects(output, sections,
        provenance["section_accounting"]["resident_ram_sections"]), config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--reference-root", required=True, type=Path)
    parser.add_argument("--lean-root", required=True, type=Path)
    parser.add_argument("--readelf", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    record = json.loads(args.report.read_text())
    record["ram_attribution"] = {"schema": 1, "cohorts": {}}
    for cohort, root in (("reference", args.reference_root), ("lean", args.lean_root)):
        record["ram_attribution"]["cohorts"][cohort] = {
            key: frozen_attribution(case, root / key, args.readelf)
            for key, case in record[cohort]["cases"].items()}
    args.out.write_text(json.dumps(record, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
