"""Host contract witnesses; these do not claim qualification on a live target."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("gdb_export", Path(__file__).parents[1] / "gdb_export.py")
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class TargetType:
    def __init__(self, fields=(), size=4, length=None):
        self._fields = [SimpleNamespace(name=name, bitpos=index * 32)
                        for index, name in enumerate(fields)]
        self.sizeof = size
        self.length = length

    def strip_typedefs(self):
        return self

    def fields(self):
        return self._fields

    def pointer(self):
        return TargetType(size=4)

    def range(self):
        return (0, self.length - 1)


class TargetArray(list):
    @property
    def type(self):
        return TargetType(length=len(self))


class TargetPointer:
    def __init__(self, address, fields):
        self.address = address
        self._fields = fields

    def __int__(self):
        return self.address

    def __bool__(self):
        return bool(self.address)

    def __getitem__(self, key):
        return self._fields[key]


class Chunk(dict):
    def __init__(self, address, size_flags, preceding=0, **extra):
        super().__init__(size=size_flags, preceding=preceding, **extra)
        self.address = address
        self.nextnode = None

    @property
    def nodesize(self):
        return self["size"] & ~3


class Heap(dict):
    """An independent literal heap with guard/allocated/free/guard nodes."""

    def __init__(self, debug=False):
        # Bounds: 0x1000..0x1100. Header size is 8 without optional tracing.
        header = 16 if debug else 8
        extra = {"pid": 42, "seqno": 7} if debug else {}
        first = Chunk(0x1000, header | 1)
        allocated = Chunk(0x1000 + header, 0x40 | 1, **extra)
        free_start = allocated.address + 0x40
        last_address = 0x1100 - header
        free = Chunk(free_start, last_address - free_start)
        last = Chunk(last_address, header | 1 | 2, preceding=free.nodesize)
        first.nextnode, allocated.nextnode, free.nextnode = allocated, free, last
        self.chunks = [first, allocated, free, last]
        super().__init__(
            mm_heapstart=TargetArray([first]), mm_heapend=TargetArray([last]),
            mm_nodelist=TargetArray([]), mm_delaylist=TargetArray([0]),
        )
        self.address = 0x800
        self.name = "User heap"
        self.nregions = 1
        self.regions = [(first, last)]
        self.type = TargetType(fields=self.keys())


class GdbTarget:
    def __init__(self, elf, heap, debug=False):
        self.heap = heap
        self.thread = SimpleNamespace(num=1, is_stopped=lambda: True)
        self.inferior = SimpleNamespace(num=1, threads=lambda: [self.thread])
        self._inferiors = [self.inferior]
        self.progspace = SimpleNamespace(filename=str(elf))
        self.types = {
            "struct mm_heap_s": TargetType(fields=heap.keys()),
            "struct mm_allocnode_s": TargetType(
                fields=("preceding", "size", "pid", "seqno") if debug else ("preceding", "size"),
                size=16 if debug else 8,
            ),
            "mmsize_t": TargetType(size=4), "void": TargetType(size=0),
        }
        self.evaluated = []

    def inferiors(self):
        return self._inferiors

    def selected_inferior(self):
        return self.inferior

    def current_progspace(self):
        return self.progspace

    def lookup_type(self, name):
        return self.types[name]

    def parse_and_eval(self, expression):
        self.evaluated.append(expression)
        if expression == "g_npidhash":
            return 8
        if expression == "g_mmheap":
            return self.heap
        raise AssertionError(f"Unexpected GDB expression: {expression}")


def generate_synthetic_capture(elf_path, output_path):
    """Generate fake GDB evidence for make_elf_fixture.py's addressed heap.

    The supplied ELF is real and supplies identity/class; every target object
    here is synthetic. Only this test helper replaces its generated JSON output.
    Production write_snapshot() continues to refuse overwriting captures.
    """
    elf_path, output_path = Path(elf_path).resolve(), Path(output_path).resolve()
    if elf_path == output_path:
        raise ValueError("The synthetic capture output must differ from its ELF")
    word = exporter.elf_identity(elf_path)["elf_class_bits"] // 8
    header = 2 * word
    heap = Heap()
    heap.name = "Synthetic fixture heap"
    first, allocated, free, last = heap.chunks
    first.address, first["size"] = 0x20001000, header | 1
    allocated.address, allocated["size"] = first.address + header, 0x400 | 1
    free.address = allocated.address + 0x400
    last.address = 0x20002000 - header
    free["size"] = last.address - free.address
    last["size"], last["preceding"] = header | 1 | 2, free.nodesize
    heap.address = 0x20000100
    gdb = GdbTarget(elf_path, heap)
    gdb.types["struct mm_allocnode_s"] = TargetType(fields=("preceding", "size"), size=header)
    gdb.types["struct mm_allocnode_s"]._fields[1].bitpos = word * 8
    gdb.types["mmsize_t"] = TargetType(size=word)
    gdb.types["void"].pointer = lambda: TargetType(size=word)
    stack = allocated.address + header
    tcb = TargetPointer(0x20000200, {
        "pid": 42, "name": "synthetic-worker", "stack_alloc_ptr": stack,
        "stack_base_ptr": stack, "adj_stack_size": 0x200,
    })
    mm = SimpleNamespace(MMHeap=lambda value, name=None: value)
    utils = SimpleNamespace(
        gdb_eval_or_none=lambda name: heap if name == "g_mmheap" else None,
        get_tcbs=lambda: [tcb], get_task_name=lambda task: task["name"],
    )
    document = exporter.collect_snapshot(
        gdb, mm, utils, label="Synthetic exporter contract witness", time_ns=0,
    )
    document["capture"]["evidence_kind"] = "synthetic GDB objects, not a device capture"
    document["warnings"].insert(0, "Synthetic exporter contract witness, not live firmware evidence.")
    output_path.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return document


class GdbExporterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # Only identity and ELF class are read by the exporter. Actual ELF layout
        # parsing has a separate importer fixture and is not simulated here.
        self.elf = self.root / "nuttx.elf"
        self.elf.write_bytes(b"\x7fELF\x01\x01\x01" + bytes(9) + b"identity-fixture")
        self.heap = Heap()
        self.gdb = GdbTarget(self.elf, self.heap)
        self.tasks = []
        self.mm = SimpleNamespace(MMHeap=lambda value, name=None: value)
        self.utils = SimpleNamespace(
            gdb_eval_or_none=lambda name: self.heap if name == "g_mmheap" else None,
            get_tcbs=lambda: self.tasks,
            get_task_name=lambda tcb: tcb["name"],
        )

    def capture(self, **options):
        return exporter.collect_snapshot(self.gdb, self.mm, self.utils, **options)

    def test_raw_chunks_guards_hash_and_evidence_limits(self):
        document = self.capture(label="ready", time_ns=123)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["build"]["elf_sha256"], hashlib.sha256(self.elf.read_bytes()).hexdigest())
        self.assertEqual(document["regions"][0]["start"], 0x1000)
        self.assertEqual(document["regions"][0]["size"], 0x100)
        self.assertIsNone(document["regions"][0]["physical"])
        snapshot = document["snapshots"][0]
        self.assertEqual(snapshot["time_ns"], 123)
        arena, first, allocation, free, last = snapshot["spans"]
        self.assertEqual([s["kind"] for s in snapshot["spans"]],
                         ["heap_arena", "reserved", "allocation", "free", "reserved"])
        self.assertEqual(sum(s["size"] for s in [first, allocation, free, last]), arena["size"])
        self.assertEqual(allocation["start"], 0x1008)
        self.assertEqual(allocation["size"], 0x40)
        self.assertIsNone(allocation["source"])
        self.assertIn('"requested_bytes":null', allocation["detail"])
        self.assertIn('"pid":null', allocation["detail"])
        self.assertEqual(snapshot["complete_regions"], [document["regions"][0]["id"]])
        self.assertIn("unverified", document["capture"]["target_build_identity"])
        self.assertTrue(any("manually" in warning for warning in document["warnings"]))
        self.assertTrue(any("high-water" in warning for warning in document["warnings"]))
        # No evaluator expression can invoke a target function.
        self.assertEqual(self.gdb.evaluated, ["g_npidhash"])

    def test_running_thread_in_other_inferior_refuses_capture(self):
        running = SimpleNamespace(num=2, is_stopped=lambda: False)
        self.gdb._inferiors.append(SimpleNamespace(num=2, threads=lambda: [running]))
        with self.assertRaisesRegex(exporter.ExportError, "Inferior 2"):
            self.capture()

    def test_no_visible_target_threads_refuses_capture(self):
        self.gdb.inferior.threads = lambda: []
        with self.assertRaisesRegex(exporter.ExportError, "visible threads"):
            self.capture()

    def test_changed_thread_inventory_refuses_capture(self):
        def tasks_after_change():
            self.gdb.thread.num = 3
            return []
        self.utils.get_tcbs = tasks_after_change
        with self.assertRaisesRegex(exporter.ExportError, "inventory changed"):
            self.capture()

    def test_bad_previous_boundary_refuses_snapshot(self):
        self.heap.chunks[-1]["preceding"] -= 4
        with self.assertRaisesRegex(exporter.ExportError, "previous chunk size"):
            self.capture()

    def test_zero_size_and_crossing_guard_refuse_snapshot(self):
        for bad_size in (0, 0x1000):
            with self.subTest(size=bad_size):
                self.heap.chunks[1]["size"] = bad_size | 1
                with self.assertRaises(exporter.ExportError):
                    self.capture()

    def test_cycle_in_chunk_successor_refuses_snapshot(self):
        self.heap.chunks[1].nextnode = self.heap.chunks[0]
        with self.assertRaisesRegex(exporter.ExportError, "Invalid heap chunk"):
            self.capture()

    def test_adjacent_free_chunks_refuse_snapshot(self):
        self.heap.chunks[1]["size"] &= ~1
        self.heap.chunks[2]["size"] |= 2
        self.heap.chunks[2]["preceding"] = self.heap.chunks[1].nodesize
        with self.assertRaisesRegex(exporter.ExportError, "coalesced"):
            self.capture()

    def test_unsupported_allocator_layout_refuses_snapshot(self):
        self.gdb.types["struct mm_heap_s"] = TargetType(fields=("tlsf", "lock"))
        with self.assertRaisesRegex(exporter.ExportError, "Unsupported allocator"):
            self.capture()

    def test_deferred_free_stays_allocated_without_drain(self):
        pending = TargetPointer(0x1010, {"flink": 0})
        self.heap["mm_delaylist"][0] = pending
        document = self.capture()
        allocation = document["snapshots"][0]["spans"][2]
        self.assertEqual(allocation["kind"], "allocation")
        self.assertIn('"deferred_free":true', allocation["detail"])
        self.assertEqual(document["capture"]["heaps"][0]["deferred_free_nodes"], 1)
        self.assertIs(self.heap["mm_delaylist"][0], pending)

    def test_invalid_or_cyclic_deferred_list_refuses_snapshot(self):
        pending = TargetPointer(0x1010, {})
        pending._fields["flink"] = pending
        self.heap["mm_delaylist"][0] = pending
        with self.assertRaisesRegex(exporter.ExportError, "Cyclic"):
            self.capture()
        self.heap["mm_delaylist"][0] = TargetPointer(0x1049, {"flink": 0})
        with self.assertRaisesRegex(exporter.ExportError, "does not match"):
            self.capture()

    def test_optional_debug_fields_are_read_by_actual_layout(self):
        self.heap = Heap(debug=True)
        self.gdb = GdbTarget(self.elf, self.heap, debug=True)
        document = self.capture()
        detail = document["snapshots"][0]["spans"][2]["detail"]
        self.assertIn('"pid":42', detail)
        self.assertIn('"allocation_seqno":7', detail)
        # A build can track PIDs without CONFIG_MM_BACKTRACE_SEQNO.
        self.gdb.types["struct mm_allocnode_s"]._fields.pop()
        del self.heap.chunks[1]["seqno"]
        self.assertIn('"allocation_seqno":null', self.capture()["snapshots"][0]["spans"][2]["detail"])

    def recorded_backtrace(self):
        self.heap = Heap(debug=True)
        self.gdb = GdbTarget(self.elf, self.heap, debug=True)
        self.gdb.types["struct mm_allocnode_s"]._fields.append(SimpleNamespace(name="backtrace", bitpos=128))
        self.heap.chunks[1]["backtrace"] = TargetArray([0x8001234, 0x8002345, 0])

    def test_source_uses_only_first_recorded_pc_and_preserves_raw_backtrace(self):
        self.recorded_backtrace()
        lookups = []
        def resolve(pc):
            lookups.append(pc)
            return SimpleNamespace(symtab=SimpleNamespace(filename="service/worker.rs"), line=42)
        self.gdb.find_pc_line = resolve
        self.gdb.block_for_pc = lambda pc: SimpleNamespace(function=None, superblock=SimpleNamespace(
            function=SimpleNamespace(print_name="worker::submit" if pc == 0x8001234 else "app::run")))
        document = self.capture()
        allocation = document["snapshots"][0]["spans"][2]
        self.assertEqual(allocation["source"], "service/worker.rs")
        self.assertEqual(lookups, [0x8001234, 0x8002345])
        self.assertEqual(allocation["backtrace"], [
            {"pc": 0x8001234, "function": "worker::submit", "file": "service/worker.rs", "line": 42},
            {"pc": 0x8002345, "function": "app::run", "file": "service/worker.rs", "line": 42},
        ])
        self.assertIn('"backtrace_pc":[134222388,134226757]', allocation["detail"])
        self.assertIn("first recorded backtrace frame; not current semantic owner", allocation["detail"])
        self.assertTrue(any("source colors" in warning for warning in document["warnings"]))

    def test_missing_first_frame_source_does_not_guess_from_later_pc(self):
        self.recorded_backtrace()
        lookups = []
        def resolve(pc):
            lookups.append(pc)
            return SimpleNamespace(symtab=None if pc == 0x8001234 else SimpleNamespace(filename="later.rs"), line=0)
        self.gdb.find_pc_line = resolve
        allocation = self.capture()["snapshots"][0]["spans"][2]
        self.assertIsNone(allocation["source"])
        self.assertEqual(lookups, [0x8001234, 0x8002345])
        self.assertEqual(allocation["backtrace"], [
            {"pc": 0x8001234, "function": None, "file": None, "line": None},
            {"pc": 0x8002345, "function": None, "file": "later.rs", "line": None},
        ])
        self.assertIn('"backtrace_pc":[134222388,134226757]', allocation["detail"])

    def test_backtrace_symbol_and_line_failures_are_independent(self):
        self.recorded_backtrace()
        def location(pc):
            if pc == 0x8001234:
                raise RuntimeError("no line table")
            return SimpleNamespace(symtab=SimpleNamespace(filename="later.rs"), line=7)
        def function(pc):
            if pc == 0x8002345:
                raise RuntimeError("no block")
            return SimpleNamespace(function=SimpleNamespace(print_name="inlined::caller"))
        self.gdb.find_pc_line = location
        self.gdb.block_for_pc = function
        allocation = self.capture()["snapshots"][0]["spans"][2]
        self.assertEqual(allocation["backtrace"], [
            {"pc": 0x8001234, "function": "inlined::caller", "file": None, "line": None},
            {"pc": 0x8002345, "function": None, "file": "later.rs", "line": 7},
        ])
        self.assertIsNone(allocation["source"])

    def task(self, address=0x900, base=0x1010, size=0x30, alloc=0x1010):
        return TargetPointer(address, {"pid": 42, "name": "worker", "stack_alloc_ptr": alloc,
                                       "stack_base_ptr": base, "adj_stack_size": size})

    def test_stack_bounds_are_child_and_live_usage_unknown(self):
        self.tasks.append(self.task())
        document = self.capture()
        stack = document["snapshots"][0]["spans"][-1]
        self.assertEqual((stack["kind"], stack["start"], stack["size"]), ("stack", 0x1010, 0x30))
        self.assertEqual(stack["parent"], document["snapshots"][0]["spans"][2]["id"])
        task = document["capture"]["tasks"][0]
        self.assertIsNone(task["current_sp"])
        self.assertIsNone(task["high_water_bytes"])
        self.assertFalse(any(span["kind"] == "stack_used" for span in document["snapshots"][0]["spans"]))

    def test_shared_boundary_or_unknown_stack_does_not_invent_extent(self):
        # Usable allocator capacity can reach a word into the next header.
        # The v1 parent model cannot represent that overlap, so retain raw TCB
        # facts and mark the stack span unavailable rather than shortening it.
        self.tasks.append(self.task(size=0x3C))
        document = self.capture()
        self.assertFalse(any(span["kind"] == "stack" for span in document["snapshots"][0]["spans"]))
        task = document["capture"]["tasks"][0]
        self.assertEqual(task["adj_stack_size"], 0x3C)
        self.assertIsNone(task["span"])
        self.assertIn("not contained", task["unavailable_reason"])

    def test_external_stack_is_addressed_but_not_a_complete_heap(self):
        self.tasks.append(self.task(base=0x2000, size=0x100, alloc=0x1FF0))
        document = self.capture()
        self.assertEqual(len(document["regions"]), 2)
        stack = document["snapshots"][0]["spans"][-1]
        self.assertEqual(stack["start"], 0x2000)
        self.assertIsNone(stack["parent"])
        self.assertNotIn(stack["region"], document["snapshots"][0]["complete_regions"])

    def test_registry_cycle_is_bounded(self):
        entry = TargetPointer(0x300, {"name": SimpleNamespace(string=lambda: "heap"), "heap": self.heap})
        entry._fields["next"] = entry
        self.utils.gdb_eval_or_none = lambda name: entry
        with self.assertRaisesRegex(exporter.ExportError, "heap registry"):
            self.capture()

    def test_explicit_heap_symbol_rejects_function_expression(self):
        with self.assertRaisesRegex(exporter.ExportError, "not a GDB expression"):
            self.capture(heap_symbols=["mm_malloc(g_mmheap, 100)"])
        self.assertEqual(self.gdb.evaluated, [])

    def test_bad_clock_coordinate_and_host_elf_fail(self):
        for coordinate in (-1, 1 << 64):
            with self.assertRaisesRegex(exporter.ExportError, "unsigned 64-bit"):
                self.capture(time_ns=coordinate)
        self.elf.write_bytes(b"not-an-ELF" * 5)
        with self.assertRaisesRegex(exporter.ExportError, "not a supported ELF"):
            self.capture()

    def test_write_roundtrip_and_existing_file_preserved(self):
        path = self.root / "capture.json"
        document = self.capture()
        exporter.write_snapshot(path, document)
        self.assertEqual(json.loads(path.read_text()), document)
        with self.assertRaises(FileExistsError):
            exporter.write_snapshot(path, {"replaced": True})
        self.assertEqual(json.loads(path.read_text()), document)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--write-synthetic":
        parser = argparse.ArgumentParser(description="Generate explicitly synthetic GDB evidence for the ELF fixture")
        parser.add_argument("--write-synthetic", nargs=2, metavar=("ELF", "OUTPUT"), required=True)
        args = parser.parse_args()
        document = generate_synthetic_capture(*args.write_synthetic)
        print(f"Wrote synthetic exporter capture; host ELF SHA256={document['build']['elf_sha256']}")
    else:
        unittest.main()
