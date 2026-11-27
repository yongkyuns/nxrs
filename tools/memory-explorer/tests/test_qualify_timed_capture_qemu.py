"""Synthetic host controls for native evidence admission, not native proof."""

from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
        sources = [PATCH, *COPIED.values(), *BUILDERS,
                   *(f"{FIXTURE}/{name}" for name in fixture_names)]
        for source in sources:
            path = self.repository / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic source identity: {source}\n")
        fixture_hashes = {name: self.digest(f"{FIXTURE}/{name}") for name in fixture_names}
        self.build = {
            "production_patch": {"path": PATCH, "sha256": self.digest(PATCH)},
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
        }

    def digest(self, source):
        return hashlib.sha256((self.repository / source).read_bytes()).hexdigest()

    def test_current_source_and_installed_identities_are_admitted(self):
        validate_build_sources(self.build, self.repository)

    def test_stale_production_fixture_copied_or_builder_source_is_rejected(self):
        for source, message in (
            (PATCH, "current production capture patch"),
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


if __name__ == "__main__":
    unittest.main()
