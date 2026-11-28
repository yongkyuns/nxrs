"""Synthetic host controls for native evidence admission, not native proof."""

from copy import deepcopy
import hashlib
import io
import itertools
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from build_timed_capture_nuttx import SYSTICK_PATCH, SYSTICK_SOURCE
from build_timing_nuttx import (ATOMIC_STACK_AFTER, ATOMIC_STACK_BEFORE,
                                ATOMIC_STACK_PATCH, ATOMIC_STACK_SOURCE)
import qualify_timed_capture_qemu as timed_capture
from qualify_timed_capture_qemu import native_evidence, validate_build_sources


RECORDING_ID = "1234567890abcdef1234567890abcdef"
PATCH = "upstream/nuttx-apps/patches/0007-trace-capture-window.patch"
FIXTURE = "tests/nuttx-std/timed-capture"
COPIED = {
    "acquisition_trace.c": "shared/acquisition/src/platform/nuttx/ffi/trace.c",
    "qemu_timer_clock.c": "tests/nuttx-std/memory-timing/qemu_timer_clock.c",
}
BUILDERS = (
    "tools/memory-explorer/tests/build_timed_capture_nuttx.py",
    "tools/memory-explorer/tests/build_timing_nuttx.py",
)


def checksum(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def synthetic_console(duplicate_operation=False):
    """Make a tiny explicit ARM-note fixture with independent work receipts.

    The negative case replaces the second 1 Hz operation's entire marker
    triplet with the first operation's triplet. Both the raw and text streams
    are reordered and rechecksummed, retaining their original record counts.
    The six distinct work receipts remain unchanged.
    """
    enabled_ticks = 20_000_010
    operations = []
    events = []
    for source in (1, 2):
        for sequence in range(1, 2 * source + 1):
            due = enabled_ticks + 4_000_000 + (sequence - 1) * 20_000_000 // source
            first = due + source * 100
            row = {
                "source": source,
                "sequence": sequence,
                "due_ticks": due,
                "begin_before_ticks": first,
                "begin_after_ticks": first + 2,
                "end_before_ticks": first + 30,
                "end_after_ticks": first + 32,
                "fix_before_ticks": first + 40,
                "fix_after_ticks": first + 42,
                "checksum": f"{checksum(bytes((source * 41 + sequence * 17 + index) & 255 for index in range(256))):08x}",
            }
            operations.append(row)
            if duplicate_operation and source == 1 and sequence == 2:
                first -= 20_000_000
            for edge, offset, stage in (
                ("B", 1, "read_parse_deliver"),
                ("E", 31, "read_parse_deliver"),
                ("I", 41, "fix_received"),
            ):
                events.append((first + offset, edge, f"gnss/simulated_{source}hz/{stage}"))
    events.sort()
    raw = []
    text = []
    for ticks, edge, label in events:
        encoded = label.encode("ascii")
        record = bytearray(32 + len(encoded))
        record[0] = len(record)
        record[1] = {"B": 31, "E": 32, "I": 33}[edge]
        record[4:8] = (4).to_bytes(4, "little")
        record[8:16] = ticks.to_bytes(8, "little")
        record[24:24 + len(encoded)] = encoded
        raw.append(bytes(record))
        seconds, fraction = divmod(ticks * 50, 1_000_000_000)
        text.append(f"worker-4 [0] {seconds}.{fraction:09d}: tracing_mark_write: {edge}|4|{label}")
    rows = [
        "NXRS_NOTE_BEGIN schema=3 source=nuttx-noteram-ascii clock=nuttx-perf "
        f"cpus=1 mode=0 notes_lost=0 recording_id={RECORDING_ID} categories=app "
        "frequency_hz=20000000 capture_start_ns=1000000000 capture_end_ns=3000000000 "
        "requested_duration_ns=2000000000 stop_reason=duration buffer_bytes=32768",
        *text,
        "",
        f"NXRS_NOTE_END schema=3 mode=0 notes_lost=0 complete=1 recording_id={RECORDING_ID}",
        "NXRS_TIMED_ORACLE schema=1 seconds=2 overflow=0 frequency_hz=20000000 "
        f"recording_id={RECORDING_ID} operation_count=6 worker_error=0 "
        "worker_stack_bytes=8192 work_bytes=256 flood_count=0 capture_status=0 "
        "call_before_ticks=19000000 call_after_ticks=61000000 "
        f"enabled_seen_ticks={enabled_ticks} disabled_seen_ticks=60000020 worker_task_id=4",
        "NXRS_TIMED_OWNER_RECOVERY signal=9 owner_exit_status=1 stop_status=0 recorder_enabled=0 owner_pid=5 stop_pid=6",
        "NXRS_TIMED_ABI common_bytes=16 clock_offset=8 clock_bytes=8 pid_offset=4 "
        "pid_bytes=4 event_bytes=32 data_offset=24 begin_type=31 end_type=32 mark_type=33 word_bytes=4",
    ]
    rows.extend("NXRS_TIMED_OPERATION " + " ".join(f"{key}={value}" for key, value in row.items())
                for row in operations)
    rows.extend(f"NXRS_TIMED_RAW index={index} bytes={record.hex()}" for index, record in enumerate(raw))
    aligned = sum((len(record) + 3) & ~3 for record in raw)
    rows.extend([
        f"NXRS_TIMED_RAW_END records={len(raw)} aligned_bytes={aligned} checksum={checksum(b''.join(raw)):08x} mode=0",
        "NXRS_TIMED_WITNESS PASS",
    ])
    return ("\n".join(rows) + "\n").encode("ascii")


class TimedConsoleCapture(unittest.TestCase):
    def capture(self, *chunks, boot=b"NuttShell\r\nnsh> \x1b[K", command_sent=True,
                timeout=10, clock_step=0.05):
        remaining = [boot, *chunks]
        consumed = []
        process = Mock(stdin=io.BytesIO(), stdout=Mock(), pid=1234)
        clock = itertools.count(0, clock_step)
        self.clock_samples = []

        def read(_descriptor, size):
            data = remaining.pop(0)
            if isinstance(data, Exception):
                raise data
            if len(data) > size:
                remaining.insert(0, data[size:])
                data = data[:size]
            consumed.append(data)
            return data

        def ready(*_args):
            return ([process.stdout] if remaining else [], [], [])

        def monotonic():
            now = next(clock)
            self.clock_samples.append(now)
            return now

        with TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(timed_capture.subprocess, "Popen", return_value=process), \
                 patch.object(timed_capture.select, "select", side_effect=ready), \
                 patch.object(timed_capture.os, "read", side_effect=read), \
                 patch.object(timed_capture.os, "killpg", create=True) as killpg, \
                 patch.object(timed_capture.signal, "SIGKILL", 9, create=True), \
                 patch.object(timed_capture.time, "monotonic", side_effect=monotonic), \
                 patch.object(timed_capture.time, "sleep"):
                try:
                    return timed_capture.capture(Path("qemu"), Path("nuttx"),
                                                 "timed_capture 2 recorded normal", output, timeout)
                finally:
                    self.retained_console = (output / "console.log").read_bytes()
                    self.remaining_chunks = remaining
                    self.assertEqual(self.retained_console, b"".join(consumed))
                    self.assertEqual(process.stdin.getvalue(),
                                     b"timed_capture 2 recorded normal\n" if command_sent else b"")
                    killpg.assert_called_once_with(process.pid, 9)
                    process.wait.assert_called_once_with(timeout=5)

    def test_complete_erase_line_prompt_finishes_without_losing_raw_bytes(self):
        # The failed native overflow capture ended with this exact suffix.
        tail = b"NXRS_TIMED_WITNESS PASS\r\nnsh> \x1b[K"
        self.assertEqual(self.capture(tail), b"NuttShell\r\nnsh> \x1b[K" + tail)

    def test_plain_and_fragmented_prompts_finish_after_the_witness(self):
        for chunks in (
            (b"NXRS_TIMED_WITNESS PASS\r\nnsh> ",),
            (b"NXRS_TIMED_WITNESS PASS\r\nnsh> \x1b", b"[", b"K"),
            (b"NXRS_TIMED_WITNESS PASS\r\nnsh", b"> "),
        ):
            with self.subTest(chunks=chunks):
                self.assertEqual(self.capture(*chunks), b"NuttShell\r\nnsh> \x1b[K" + b"".join(chunks))

    def test_prompt_alone_or_incomplete_output_keeps_the_existing_deadline(self):
        for tail in (
            b"work still running\r\nnsh> \x1b[K",
            b"NXRS_TIMED_WITNESS PASS\r\nnsh> \x1b[",
            b"NXRS_TIMED_WITNESS PASS\r\nnsh> \x1b[Kmore output",
            b"NXRS_TIMED_WITNESS PASS\r\nvalue nsh> ",
        ):
            with self.subTest(tail=tail):
                with self.assertRaisesRegex(TimeoutError, "bounded host deadline"):
                    self.capture(tail)

    def test_native_failure_is_rejected_even_with_a_complete_prompt(self):
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed"):
            self.capture(b"NXRS_TIMED_WITNESS FAIL\r\nnsh> \x1b[K")

    def test_split_assertion_retains_location_and_backtrace_then_fails(self):
        # The native boot failure was cut off at this first chunk.
        assertion = b"dump_assert_info: Assertion failed"
        location = b" at file: sched/example.c:123 task: init\r\n"
        backtrace = b"up_dump_register: PC: 10001234\r\nbacktrace: 10001234 10005678\r\n"
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed"):
            self.capture(location, backtrace, b"", boot=assertion, command_sent=False)
        self.assertEqual(self.retained_console, assertion + location + backtrace)

    def test_boot_failure_cannot_send_a_command_or_accept_later_success(self):
        success = b"\r\nNXRS_TIMED_WITNESS PASS\r\nnsh> \x1b[K"
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed"):
            self.capture(success, b"", boot=b"Assertion failed", command_sent=False)
        self.assertTrue(self.retained_console.endswith(success))

    def test_fatal_diagnostic_grace_is_bounded_even_with_continuous_output(self):
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed"):
            self.capture(*([b"diagnostic row\r\n"] * 100),
                         boot=b"Assertion failed", command_sent=False)
        self.assertIn(b"diagnostic row", self.retained_console)
        self.assertTrue(self.remaining_chunks)
        self.assertLessEqual(max(self.clock_samples), 1.2)

    def test_fatal_diagnostics_do_not_extend_the_original_deadline(self):
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed"):
            self.capture(*([b"diagnostic row\r\n"] * 100), boot=b"Assertion failed",
                         command_sent=False, timeout=0.2)
        self.assertTrue(self.remaining_chunks)
        self.assertLessEqual(max(self.clock_samples), 0.25)

    def test_fatal_diagnostics_keep_the_console_cap_and_original_failure(self):
        limit = 4 * 1024 * 1024
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed"):
            self.capture(b"x" * limit, boot=b"Assertion failed", command_sent=False,
                         clock_step=0.001)
        self.assertEqual(len(self.retained_console), limit)
        self.assertTrue(self.remaining_chunks)

    def test_fatal_diagnostic_read_error_preserves_the_native_failure(self):
        with self.assertRaisesRegex(RuntimeError, "Native boot/workload failed") as failed:
            self.capture(OSError("diagnostic pipe closed"), boot=b"Assertion failed",
                         command_sent=False)
        self.assertTrue(any("diagnostic pipe closed" in note for note in failed.exception.__notes__))


class NativeMarkerAdmission(unittest.TestCase):
    def test_synthetic_control_covers_each_independent_operation(self):
        evidence = native_evidence(synthetic_console(), 2, False, RECORDING_ID)
        self.assertEqual(evidence["independent_operations"], 6)
        self.assertEqual(evidence["matched_native_markers"], 18)
        self.assertEqual(evidence["native_note_count"], 18)
        self.assertEqual(evidence["actual_duration_ns"], 2_000_000_000)

    def test_consistent_duplicate_triplet_cannot_replace_an_operation(self):
        with self.assertRaisesRegex(RuntimeError, "duplicate one operation"):
            native_evidence(synthetic_console(duplicate_operation=True), 2, False, RECORDING_ID)


class NativeBuildIdentityAdmission(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        fixture_names = ("Kconfig", "Make.defs", "Makefile", "timed_main.c")
        sources = [PATCH, SYSTICK_PATCH, ATOMIC_STACK_PATCH, *COPIED.values(), *BUILDERS,
                   *(f"{FIXTURE}/{name}" for name in fixture_names)]
        for source in sources:
            path = self.repository / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic source identity: {source}\n")
        fixture_hashes = {name: self.digest(f"{FIXTURE}/{name}") for name in fixture_names}
        self.build = {
            "atomic_stack_patch": {
                "path": ATOMIC_STACK_PATCH, "sha256": self.digest(ATOMIC_STACK_PATCH),
                "source": ATOMIC_STACK_SOURCE, "before": ATOMIC_STACK_BEFORE,
                "after": ATOMIC_STACK_AFTER, "source_restored": True,
            },
            "production_patch": {"path": PATCH, "sha256": self.digest(PATCH)},
            "systick_patch": {"path": SYSTICK_PATCH, "sha256": self.digest(SYSTICK_PATCH),
                              "source": SYSTICK_SOURCE},
            "fixture_sha256": fixture_hashes,
            "installed_fixture_sha256": dict(fixture_hashes),
            "copied_sources": {
                name: {"source": source, "sha256": self.digest(source),
                       "installed_sha256": self.digest(source)}
                for name, source in COPIED.items()
            },
            "build_sources_sha256": {source: self.digest(source) for source in BUILDERS},
            "source_identity_before_patch": {
                name: {"tracked_sources_clean": True} for name in ("nuttx", "apps")
            },
            "apps_tracked_sources_restored": True,
            "nuttx_tracked_sources_restored": True,
        }

    def digest(self, source):
        return hashlib.sha256((self.repository / source).read_bytes()).hexdigest()

    def test_current_source_and_installed_identities_are_admitted(self):
        validate_build_sources(self.build, self.repository)

    def test_stale_production_fixture_copied_or_builder_source_is_rejected(self):
        for source, message in (
            (ATOMIC_STACK_PATCH, "atomic stack patch/source identity differs"),
            (PATCH, "current production capture patch"),
            (SYSTICK_PATCH, "current production SysTick patch"),
            (f"{FIXTURE}/timed_main.c", "fixture source/installed identity"),
            *((source, "bridge/clock source identity") for source in COPIED.values()),
            *((source, "helper identity") for source in BUILDERS),
        ):
            with self.subTest(source=source):
                path = self.repository / source
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"changed after the native build\n")
                    with self.assertRaisesRegex(RuntimeError, message):
                        validate_build_sources(self.build, self.repository)
                finally:
                    path.write_bytes(original)

    def test_installed_fixture_bridge_or_clock_cannot_have_a_different_hash(self):
        mutations = [("installed_fixture_sha256", "timed_main.c")]
        mutations.extend(("copied_sources", name, "installed_sha256") for name in COPIED)
        for keys in mutations:
            with self.subTest(keys=keys):
                build = deepcopy(self.build)
                value = build
                for key in keys[:-1]:
                    value = value[key]
                value[keys[-1]] = "0" * 64
                with self.assertRaisesRegex(RuntimeError, "identity differs"):
                    validate_build_sources(build, self.repository)

    def test_missing_or_extra_inputs_do_not_form_an_exact_build_identity(self):
        for key in ("fixture_sha256", "installed_fixture_sha256", "copied_sources", "build_sources_sha256"):
            for operation in ("remove", "add"):
                with self.subTest(container=key, operation=operation):
                    build = deepcopy(self.build)
                    if operation == "remove":
                        del build[key][next(iter(build[key]))]
                    else:
                        build[key]["unrecorded-input"] = "0" * 64
                    with self.assertRaisesRegex(RuntimeError, "differs"):
                        validate_build_sources(build, self.repository)

    def test_atomic_stack_source_restoration_and_retained_patch_are_required(self):
        build_dir = self.repository / "native-build"
        build_dir.mkdir()
        retained = build_dir / "atomic-stack.patch"
        retained.write_bytes((self.repository / ATOMIC_STACK_PATCH).read_bytes())
        validate_build_sources(self.build, self.repository, build_dir)
        build = deepcopy(self.build)
        build["atomic_stack_patch"]["source_restored"] = False
        with self.assertRaisesRegex(RuntimeError, "atomic stack patch/source identity differs"):
            validate_build_sources(build, self.repository, build_dir)
        retained.write_bytes(retained.read_bytes() + b"changed after native build\n")
        with self.assertRaisesRegex(RuntimeError, "Retained atomic stack patch differs"):
            validate_build_sources(self.build, self.repository, build_dir)


if __name__ == "__main__":
    unittest.main()
