"""Independent native-outcome admission controls, using labeled synthetic rows."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from build_requests_nuttx import patch_paths, run as build, validate_apps_directory
from qualify_requests_qemu import CASES, NSH_PROMPT, controls, evidence


RECORDING = "0123456789abcdef0123456789abcdef"


def synthetic_console():
    """Explicit native-call facts, not output from the converter or allocator."""
    old = [0, 0, 0, 0x2000, 0x2000, 0, 0x2000, 0x4000, 0, 0, 0x7000, 0]
    returned = [0x2000, 0x2000, 0, 0x2000, 0x2000, 0x2800, 0x4000, 0, 0x6000, 0x7000, 0x7000, 0x8000]
    nested = {6: (8192, 0x4000), 7: (131072, 0), 8: (768, 0x5fc0), 9: (256, 0x7000)}
    rows, notes = [], []
    counter, request_id = 100, 701

    def note(ticks, payload):
        ns = ticks * 50
        return f"heap_requests-42 [0] {ns // 1000000000}.{ns % 1000000000:09}: {payload}"

    def native_pointer(value):
        return f"{value:#x}" if value else "0"

    for index, (name, operation, size, alignment) in enumerate(CASES):
        before, after = 1000 + index * 1000, 1900 + index * 1000
        count = 2 if index in nested else 1
        phase = "disabled_before" if index == 0 else "disabled_after" if index == 11 else "enabled"
        rows.append(f"NXRS_REQUEST_ROW index={index} case={name} phase={phase} op={operation} size={size} "
                    f"align={alignment} old={old[index]:#x} result={returned[index]:#x} before_count={counter} "
                    f"after_count={counter + count} before_ticks={before} after_ticks={after} payload_preserved=1")
        counter += count
        if phase != "enabled":
            continue
        outer_id = request_id
        request_id += 7  # Intentionally neither contiguous nor equal to the per-heap counter.
        notes.append(note(before + 100, f"heap_request: {operation} heap: 0x1000 id:{outer_id} size:{size} "
                          f"caller: 0x9001 old: {native_pointer(old[index])} align:{alignment}"))
        if index in nested:
            child_size, child_result = nested[index]
            inner_id = request_id
            request_id += 7
            notes.append(note(before + 200, f"heap_request: malloc heap: 0x1000 id:{inner_id} size:{child_size} "
                              "caller: 0xa001 old: 0 align:0"))
            notes.append(note(before + 600, f"heap_result: malloc heap: 0x1000 id:{inner_id} "
                              f"result: {native_pointer(child_result)} outcome:{'success' if child_result else 'failure'}"))
        notes.append(note(before + 800, f"heap_result: {operation} heap: 0x1000 id:{outer_id} "
                          f"result: {native_pointer(returned[index])} outcome:{'success' if returned[index] else 'failure'}"))
    return ("\n".join([
        "NXRS_REQUEST_CLOCK frequency_hz=20000000 before=1 after=100",
        "NXRS_NOTE_BEGIN schema=1 source=nuttx-noteram-ascii clock=qemu-an521-cmsdk-timer1-20mhz "
        "clock_bits=32 clock_epoch=fixture-invocation cpus=1 mode=0 notes_lost=0 frequency_hz=20000000 "
        f"recording_id={RECORDING}",
        *notes,
        f"NXRS_NOTE_END schema=1 mode=0 notes_lost=0 complete=1 recording_id={RECORDING}",
        *rows,
        f"NXRS_HEAP_REQUESTS PASS recording_id={RECORDING} heap=0x1000 owner=42 rows=12 enabled_rows=10 "
        "disabled_rows=2 dump_bytes=4096 request_note_bytes=48 result_note_bytes=32 fixture_rows_bytes=864",
    ]) + "\n").encode()


class RequestQualificationTests(unittest.TestCase):
    def test_completion_requires_a_full_prompt_with_optional_erase_line(self):
        prefix = b"NXRS_HEAP_REQUESTS PASS\r\n"
        for prompt in (b"nsh>", b"nsh> \x1b[K", b"nsh> \x1b[K\r\n"):
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(NSH_PROMPT.search(prefix + prompt))
        for prompt in (b"", b"nsh", b"nsh> \x1b[", b"nsh> \x1b[2K", b"not-nsh>"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(NSH_PROMPT.search(prefix + prompt))

    def test_native_bare_zero_and_prefixed_zero_are_equivalent(self):
        console = synthetic_console()
        self.assertIn(b"old: 0 align:", console)
        self.assertIn(b"result: 0 outcome:failure", console)
        prefixed = console.replace(b"old: 0 align:", b"old: 0x0 align:").replace(
            b"result: 0 outcome:", b"result: 0x0 outcome:")
        self.assertEqual(evidence(console), evidence(prefixed))

    def test_real_call_contract_includes_nested_results_failures_and_zero(self):
        report = evidence(synthetic_console(), RECORDING)
        self.assertEqual((report["calls"], report["nested_calls"], report["disabled_rows"]), (14, 4, 2))
        self.assertEqual([item["case"] for item in report["outer_calls"]], [case[0] for case in CASES[1:-1]])
        zero = next(item for item in report["outer_calls"] if item["case"] == "realloc_zero")
        self.assertEqual((zero["outcome"], zero["result"]), ("success", 0x7000))
        self.assertNotEqual(report["outer_calls"][0]["request_id"], report["rows"][1]["before_count"] + 1)

    def test_independent_return_pointer_catches_false_result(self):
        altered = synthetic_console().replace(b"id:701 result: 0x2000", b"id:701 result: 0x2100")
        with self.assertRaisesRegex(RuntimeError, "independently retained"):
            evidence(altered)

    def test_failed_realloc_must_preserve_old_payload(self):
        lines = synthetic_console().decode().splitlines()
        lines = [line.replace("payload_preserved=1", "payload_preserved=0")
                 if line.startswith("NXRS_REQUEST_ROW index=7 ") else line for line in lines]
        with self.assertRaisesRegex(RuntimeError, "payload preservation"):
            evidence("\n".join(lines).encode())

    def test_zero_size_is_not_misreported_as_release(self):
        lines = synthetic_console().decode().splitlines()
        lines = [line.replace("result=0x7000", "result=0x0")
                 if line.startswith("NXRS_REQUEST_ROW index=10 ") else line for line in lines]
        with self.assertRaisesRegex(RuntimeError, "success/failure"):
            evidence("\n".join(lines).encode())

    def test_missing_one_or_both_edges_are_detected(self):
        console = synthetic_console()
        derived = controls(console, evidence(console))
        self.assertEqual(set(derived), {"missing_request", "missing_result", "missing_pair"})
        self.assertIn("counter", derived["missing_pair"]["rejected_by_native_oracle"])
        for item in derived.values():
            self.assertIn("Host-derived removal", item["scope"])
            with self.assertRaises(RuntimeError):
                evidence(item["console"])

    def test_duplicate_result_and_cross_task_result_are_rejected(self):
        lines = synthetic_console().decode().splitlines()
        index = next(i for i, line in enumerate(lines) if "heap_result: malloc heap: 0x1000 id:701 " in line)
        duplicate = "\n".join(lines[:index] + [lines[index]] + lines[index:]).encode()
        with self.assertRaisesRegex(RuntimeError, "duplicate|misnested"):
            evidence(duplicate)
        lines[index] = lines[index].replace("heap_requests-42", "heap_requests-43")
        with self.assertRaisesRegex(RuntimeError, "another task"):
            evidence("\n".join(lines).encode())

    def test_disabled_before_probe_cannot_be_erased_or_admitted(self):
        lines = synthetic_console().decode().splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("NXRS_NOTE_BEGIN ")) + 1
        unwanted = [
            "heap_requests-42 [0] 0.000055000: heap_request: malloc heap: 0x1000 id:5 size:17 caller: 0x9001 old: 0x0 align:0",
            "heap_requests-42 [0] 0.000090000: heap_result: malloc heap: 0x1000 id:5 result: 0x2000 outcome:success",
        ]
        with self.assertRaisesRegex(RuntimeError, "disabled filter"):
            evidence("\n".join(lines[:index] + unwanted + lines[index:]).encode())

    def test_counter_timestamp_and_identity_contradictions_are_rejected(self):
        console = synthetic_console()
        for before, after in [
            (b"before_count=101 after_count=102", b"before_count=101 after_count=103"),
            (b"0.000140000: heap_result", b"0.000100000: heap_result"),
            (b"id:708 size:", b"id:701 size:"),
        ]:
            self.assertIn(before, console)
            with self.subTest(after=after), self.assertRaises(RuntimeError):
                evidence(console.replace(before, after, 1))

    def test_configuration_loss_or_incomplete_dump_is_not_green(self):
        console = synthetic_console()
        for before, after in [(b"notes_lost=0", b"notes_lost=1"),
                              (b"complete=1", b"complete=0"),
                              (b"request_note_bytes=48", b"request_note_bytes=256")]:
            with self.subTest(after=after), self.assertRaises(RuntimeError):
                evidence(console.replace(before, after, 1))
        with self.assertRaisesRegex(RuntimeError, "recording/reset"):
            evidence(console, "f" * 32)


class RequestBuildTests(unittest.TestCase):
    def test_configured_apps_accepts_equivalent_relative_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nuttx, apps = root / "nuttx", root / "apps"
            nuttx.mkdir()
            apps.mkdir()
            (apps / "Makefile").write_text("# pinned apps fixture\n")
            for configuration in ('CONFIG_APPS_DIR="../apps"\n',
                                  'CONFIG_APPS_DIR="../apps/./"\n',
                                  f'CONFIG_APPS_DIR="{apps.resolve().as_posix()}"\n',
                                  'CONFIG_APPS_DIR=../apps\n',
                                  'CONFIG_APPS_DIR=\n', ''):
                with self.subTest(configuration=configuration):
                    self.assertEqual(validate_apps_directory(nuttx, apps, configuration), apps.resolve())

    def test_mismatched_configured_apps_is_rejected_before_any_source_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nuttx, apps, other = (root / name for name in ("nuttx", "apps", "other-apps"))
            for path in (nuttx, apps, other):
                path.mkdir()
            for path in (apps, other):
                (path / "Makefile").write_text("# distinct source tree\n")
            configuration = 'CONFIG_ARCH_BOARD_MPS2_AN521=y\nCONFIG_APPS_DIR="../other-apps"\n'
            (nuttx / ".config").write_text(configuration)
            args = SimpleNamespace(nuttx=nuttx, apps=apps, output=root / "unreached-output")
            with mock.patch("build_requests_nuttx.source_identity") as identity, \
                 mock.patch("build_requests_nuttx.subprocess.run") as execute, \
                 mock.patch("build_requests_nuttx.shutil.copytree") as install:
                with self.assertRaisesRegex(RuntimeError, "configured apps directory.*differs from --apps"):
                    build(args)
                identity.assert_not_called()
                execute.assert_not_called()
                install.assert_not_called()
            self.assertEqual((nuttx / ".config").read_text(), configuration)
            self.assertFalse(args.output.exists())
            self.assertEqual(list(apps.iterdir()), [apps / "Makefile"])

    def test_configured_apps_does_not_evaluate_expansions_or_admit_missing_makefile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nuttx, apps = root / "nuttx", root / "apps"
            nuttx.mkdir()
            apps.mkdir()
            for value in ('"$(APPS)"', '"`pwd`/apps"', '"../apps;true"', '""', r'"..\/apps"'):
                with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "literal path"):
                    validate_apps_directory(nuttx, apps, f"CONFIG_APPS_DIR={value}\n")
            with self.assertRaisesRegex(RuntimeError, "readable Makefile"):
                validate_apps_directory(nuttx, apps, 'CONFIG_APPS_DIR="../apps"\n')

    def test_patch_inventory_requires_unique_same_target_paths(self):
        self.assertEqual(patch_paths("diff --git a/mm/a.c b/mm/a.c\n"), ["mm/a.c"])
        for source in ("diff --git a/mm/a.c b/mm/b.c\n", "diff --git a/../a b/../a\n",
                       "diff --git a/mm/a.c b/mm/a.c\ndiff --git a/mm/a.c b/mm/a.c\n"):
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                patch_paths(source)

    def test_repository_submodules_are_not_disposable_patch_destinations(self):
        repo = Path(__file__).resolve().parents[3]
        args = SimpleNamespace(nuttx=repo / "external/nuttx", apps=repo / "external/nuttx-apps",
                               output=repo / "target/unreached-requests-test")
        with self.assertRaisesRegex(RuntimeError, "disposable source clones"):
            build(args)


if __name__ == "__main__":
    unittest.main()
