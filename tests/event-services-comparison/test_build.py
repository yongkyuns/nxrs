"""Build-command and provenance regressions; no SDK build or device required."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "event_services_build_tests", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)
NATIVE_CONFIG = 'CONFIG_TEST="same-kernel"\nCONFIG_USEC_PER_TICK=10000\nCONFIG_RR_INTERVAL=10\n'


def section_table():
    return ("  [ 1] .text PROGBITS 40374000 001000 000100 00 AX 0 0 16\n")


def create_native_artifacts(argv, config=NATIVE_CONFIG):
    stage = Path(argv[argv.index("--out") + 1])
    stage.mkdir(parents=True, exist_ok=True)
    prefix = "rust" if "relink_rust.py" in " ".join(map(str, argv)) else "c"
    image = bytes((0xE9, 0, 2, 0x40))
    (stage / f"{prefix}.elf").write_bytes(b"elf")
    (stage / f"{prefix}.unpadded.bin").write_bytes(image)
    (stage / f"{prefix}.merged.bin").write_bytes(image + b"\xff" * 4)
    (stage / "resolved.config").write_text(config)
    provenance = {"test_fixture": True}
    name = "relink-provenance.json" if prefix == "rust" else "c-build-provenance.json"
    (stage / name).write_text(json.dumps(provenance))


class InventoryTests(unittest.TestCase):
    def test_arithmetic_remains_portable_and_has_no_workaround_feature(self):
        core = (build.HERE / "core.rs").read_text()
        self.assertIn("value.rotate_left(5)", core)
        self.assertNotIn("asm!", core)
        self.assertNotIn("work_xtensa", core)
        self.assertFalse((build.HERE / "work_xtensa.rs").exists())
        for manifest in (build.HERE / "Cargo.toml",
                         build.HERE.parent / "service-footprint/Cargo.toml"):
            self.assertNotIn("portable-work", manifest.read_text())

    def test_native_and_conflicting_chunk_controls_are_rejected_before_build(self):
        from types import SimpleNamespace
        for platform, policy, mode in (("nuttx-c", "natural", "monolithic"),
                                       ("zephyr-c", "event", "chunked"),
                                       ("embassy", "natural", "chunked")):
            args = SimpleNamespace(platform=platform, embassy_scheduling=policy, work_mode=mode)
            with self.subTest(platform=platform, policy=policy), self.assertRaises(ValueError):
                build.build(args)

    def test_inventory_tracks_firmware_inputs_and_excludes_test_and_build_output(self):
        inventory = build.inventory()
        required = {
            "platform.h", "contract.h", "runtime.h", "runtime.c", "core.c",
            "core.rs", "platform_nuttx.c", "platform_zephyr.c", "nuttx.rs",
            "zephyr_main.c", "CMakeLists.txt", "Cargo.toml", "Cargo.lock",
            "../zephyr-comparison/prj.conf", "../zephyr-comparison/app.overlay",
            "../embassy-comparison/stack.x", "../service-footprint/Cargo.toml",
            "../rtos_harness/images.py", "../rtos_harness/zephyr.py",
        }
        self.assertTrue(required.issubset(inventory), required - set(inventory))
        self.assertNotIn("test_build.py", inventory)
        self.assertNotIn("../rtos_harness/pins.py", inventory)
        self.assertNotIn("../zephyr-comparison/build.py", inventory)
        self.assertFalse(any("/target/" in name or name.startswith("target/")
                             for name in inventory))
        for name in required:
            path = (build.HERE / name).resolve()
            self.assertEqual(inventory[name], build.digest(path), name)

    def test_nuttx_event_services_binary_is_registered_and_selectable(self):
        manifest = tomllib.loads(
            (build.HERE.parent / "service-footprint" / "Cargo.toml").read_text())
        event_bins = [item for item in manifest.get("bin", [])
                      if item.get("name") == "event-services"]
        self.assertEqual(len(event_bins), 1)
        self.assertEqual(event_bins[0]["path"], "../event-services-comparison/nuttx.rs")
        relink_source = (build.HERE.parent / "service-footprint" / "relink_rust.py").read_text()
        self.assertRegex(relink_source, r"choices=\([^\n]*'event-services'\)")


class CommandTests(unittest.TestCase):
    def test_command_stringifies_arguments_and_records_subprocess_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "out"
            out.mkdir()
            env = {"PATH": "/mock/bin"}
            completed = subprocess.CompletedProcess(["tool"], 0, stdout="build output")
            with patch.object(build.subprocess, "run", return_value=completed) as run:
                output = build.command(["tool", Path("input.c")], out, "build.log",
                                       cwd=root, env=env)
            self.assertEqual(output, "build output")
            self.assertEqual((out / "build.log").read_text(), "build output")
            self.assertEqual(run.call_args.args[0], ["tool", "input.c"])
            self.assertEqual(run.call_args.kwargs["cwd"], root)
            self.assertEqual(run.call_args.kwargs["env"], env)


class NativeBuildArgumentTests(unittest.TestCase):
    def run_nuttx(self, platform, layout, timer_ms=10, nuttx_profile="baseline", instrumentation="full"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        tree = root / "tree"
        (tree / "nuttx").mkdir(parents=True)
        (tree / "apps").mkdir()
        out = root / f"{platform}-{layout}"
        baseline = root / "baseline.config"
        config = NATIVE_CONFIG.replace("CONFIG_USEC_PER_TICK=10000", f"CONFIG_USEC_PER_TICK={timer_ms * 1000}")
        baseline.write_text(config)
        (tree / "nuttx" / ".config").write_text(baseline.read_text())
        args = type("Args", (), {
            "platform": platform, "layout": layout, "out": out,
            "readelf": root / "readelf", "nuttx_tree": tree,
            "baseline_config": baseline, "sysroot": root / "sysroot",
            "nuttx_cargo_target": root / "cargo-target",
            "timer_ms": timer_ms,
            "instrumentation": instrumentation,
            "nuttx_profile": nuttx_profile, "matched_baseline": baseline,
        })()
        calls = []
        environments = []

        def fake_run(argv, **kwargs):
            argv = list(map(str, argv))
            calls.append(argv)
            environments.append(kwargs.get("env"))
            if "build_c.py" in " ".join(argv) or "relink_rust.py" in " ".join(argv):
                create_native_artifacts(argv, config)
            stdout = section_table() if "-W" in argv and "-S" in argv else "mocked\n"
            return subprocess.CompletedProcess(argv, 0, stdout=stdout)

        with patch.object(build.subprocess, "run", side_effect=fake_run), \
             patch.object(build.minimal, "validate") as validate:
            build.build(args)
        if nuttx_profile == "minimal":
            validate.assert_called_once_with(tree.resolve() / "nuttx/.config", baseline)
        record = json.loads((out / "build-provenance.json").read_text())
        return calls, environments, record, root

    def test_lean_changes_only_instrumentation_not_kernel_or_topology(self):
        for platform in ("nuttx-c", "nuttx-rust"):
            full = self.run_nuttx(platform, "three", instrumentation="full")[2]
            calls, _, lean, _ = self.run_nuttx(platform, "three", instrumentation="lean")
            self.assertEqual(full["kernel_config_identity"], lean["kernel_config_identity"])
            self.assertEqual({k: v for k, v in full["configuration"].items() if k != "instrumentation"},
                             {k: v for k, v in lean["configuration"].items() if k != "instrumentation"})
            self.assertTrue(any("ES_LEAN=1" in call for call in calls))

    def test_minimal_profile_keeps_both_applications_at_o2_and_records_kernel_os(self):
        for platform in ("nuttx-c", "nuttx-rust"):
            with self.subTest(platform=platform):
                calls, _, record, _ = self.run_nuttx(platform, "three", 1, "minimal")
                argv = next(call for call in calls if "build_c.py" in " ".join(call)
                            or "relink_rust.py" in " ".join(call))
                self.assertEqual(argv[argv.index("--c-opt-level") + 1], "2")
                self.assertIn(str(build.HERE / "nuttx_console.c"), argv)
                self.assertEqual(record["configuration"]["kernel_opt_level"], "Os")
                self.assertEqual(record["configuration"]["console"], "event")
                self.assertFalse(record["configuration"]["psram"])
                self.assertEqual(record["configuration"]["slots"], 480)
                self.assertTrue({"nuttx-minimal.conf", "nuttx_console.c", "nuttx_profile.py"}
                                .issubset(record["source_sha256"]))
                if platform == "nuttx-c":
                    self.assertNotIn("--reuse-kernel", argv)

    def test_one_ms_control_keeps_timeslice_and_c_rust_kernel_identical(self):
        identities = []
        for platform in ("nuttx-c", "nuttx-rust"):
            calls, _, record, _ = self.run_nuttx(platform, "three", timer_ms=1)
            argv = next(call for call in calls if "build_c.py" in " ".join(call) or "relink_rust.py" in " ".join(call))
            self.assertIn("ES_TIMER_MS=1", argv)
            self.assertEqual(record["configuration"]["publication_timer_resolution_ms"], 1)
            identities.append(record["kernel_config_identity"])
        self.assertEqual(identities[0], identities[1])

    def test_nuttx_c_defines_and_layout_configuration(self):
        for layout, mailbox, queues in (("one", "1", 20), ("three", "0", 60)):
            with self.subTest(layout=layout):
                calls, environments, record, root = self.run_nuttx("nuttx-c", layout)
                argv = next(call for call in calls if "build_c.py" in " ".join(call))
                env = environments[calls.index(argv)]
                self.assertEqual(env["PATH"].split(os.pathsep)[0], str((root / "readelf").resolve().parent))
                definitions = [argv[i + 1] for i, item in enumerate(argv[:-1])
                               if item == "--c-define"]
                self.assertIn("ES_SPEED=1", definitions)
                self.assertIn(f"ES_MAILBOX={mailbox}", definitions)
                sources = [argv[i + 1] for i, item in enumerate(argv[:-1])
                           if item == "--target-c-source"]
                headers = [argv[i + 1] for i, item in enumerate(argv[:-1])
                           if item == "--target-c-header"]
                self.assertIn(str(build.HERE / "platform_nuttx.c"), sources)
                self.assertIn(str(build.HERE / "runtime.c"), sources)
                self.assertIn(str(build.HERE / "platform.h"), headers)
                self.assertEqual(record["configuration"]["queues"], queues)
                self.assertEqual(record["configuration"]["slots"], 480)

    def test_nuttx_rust_registers_new_bin_and_passes_native_defines(self):
        for layout, mailbox in (("one", "1"), ("three", "0")):
            with self.subTest(layout=layout):
                calls, environments, record, root = self.run_nuttx("nuttx-rust", layout)
                argv = next(call for call in calls if "relink_rust.py" in " ".join(call))
                env = environments[calls.index(argv)]
                self.assertIsNotNone(env)
                self.assertEqual(env["PATH"].split(os.pathsep)[0], str((root / "readelf").resolve().parent))
                self.assertEqual(argv[argv.index("--bin") + 1], "event-services")
                self.assertEqual(argv[argv.index("--command") + 1], "es_rust")
                definitions = [argv[i + 1] for i, item in enumerate(argv[:-1])
                               if item == "--c-define"]
                self.assertIn("ES_SPEED=1", definitions)
                self.assertIn(f"ES_MAILBOX={mailbox}", definitions)
                self.assertIn("ES_RUST=1", definitions)
                sources = [argv[i + 1] for i, item in enumerate(argv[:-1])
                           if item == "--target-c-source"]
                self.assertIn(str(build.HERE / "platform_nuttx.c"), sources)
                self.assertEqual(record["configuration"]["slots"], 480)

    def test_zephyr_mailbox_mapping_and_build_environment(self):
        for layout, mailbox, queues in (("one", "1", 20), ("three", "0", 60)):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ("zephyr", "espressif", "xtensa", "sdk"):
                    (root / name).mkdir()
                python = root / "venv" / "bin" / "python"
                python.parent.mkdir(parents=True)
                python.write_text("mock python\n")
                out = root / f"zephyr-{layout}"
                args = type("Args", (), {
                    "platform": "zephyr-c", "layout": layout, "out": out,
                    "readelf": root / "readelf", "zephyr": root / "zephyr",
                    "espressif": root / "espressif", "xtensa": root / "xtensa",
                    "sdk": root / "sdk", "zephyr_python": python,
                })()
                calls = []
                environments = []

                def fake_run(argv, **kwargs):
                    argv = list(map(str, argv))
                    calls.append(argv)
                    environments.append(kwargs.get("env"))
                    if "-B" in argv:
                        stage = Path(argv[argv.index("-B") + 1])
                        zephyr_build = stage / "zephyr"
                        zephyr_build.mkdir(parents=True)
                        (zephyr_build / ".config").write_text("CONFIG_TEST=y\n")
                        (zephyr_build / "zephyr.elf").write_bytes(b"elf")
                        (zephyr_build / "zephyr.bin").write_bytes(b"bin")
                    stdout = section_table() if "-W" in argv and "-S" in argv else "mocked\n"
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout)

                with patch.object(build.zephyr, "check_checkout", side_effect=[
                         build.zephyr.ZEPHYR_SHA, "test-espressif", "test-xtensa"]), \
                     patch.object(build.zephyr, "git", return_value=build.zephyr.ZEPHYR_SHA), \
                     patch.object(build.zephyr, "assert_config", return_value={"CONFIG_TEST": "y", "CONFIG_SYS_CLOCK_TICKS_PER_SEC": "100"}), \
                     patch.object(build.subprocess, "run", side_effect=fake_run):
                    build.build(args)
                configure = next(call for call in calls if "-GNinja" in call)
                env = environments[calls.index(configure)]
                self.assertIn(f"-DES_MAILBOX={mailbox}", configure)
                self.assertEqual(env["ZEPHYR_BASE"], str((root / "zephyr").resolve()))
                self.assertTrue(env["PATH"].startswith(str(python.absolute().parent)))
                record = json.loads((out / "build-provenance.json").read_text())
                self.assertEqual(record["configuration"]["queues"], queues)
                self.assertEqual(record["configuration"]["slots"], 480)


class NuttXAdapterSourceTests(unittest.TestCase):
    def test_target_macro_guards_cover_clock_heap_and_target_stack(self):
        source = (build.HERE / "platform_nuttx.c").read_text()
        self.assertNotIn("__NUTTX__", source)
        self.assertIn("#ifdef __NuttX__", source)
        self.assertRegex(
            source,
            r"#if defined\(__NuttX__\) && defined\(__XTENSA__\)\s+"
            r"return es_clock_cycles\(\);",
        )
        self.assertNotIn("rsr.ccount", source)
        self.assertIn("return (uint32_t)mallinfo().uordblks;", source)
        self.assertRegex(source, r"#ifndef __NuttX__\s+stack_bytes = ES_HOST_STACK_BYTES")
        self.assertIn("size_t stack_bytes = ES_STACK_BYTES;", source)

    def test_shared_clock_uses_system_timer_without_resetting_it(self):
        source = (build.HERE / "clock.h").read_text()
        prepare = source.split("static inline void es_clock_prepare(void) {", 1)[1]
        prepare = prepare.split("static inline uint32_t es_clock_cycles(void)", 1)[0]
        self.assertIn("*ES_SYSTIMER_CONF =", prepare)
        self.assertIn("(1u << 30)", prepare)
        self.assertIn("~((1u << 28) | (1u << 27))", prepare)
        self.assertNotIn("ES_SYSTIMER_UNIT0_OP", prepare)
        self.assertNotIn("ES_SYSTIMER_UNIT0_LO", prepare)
        source = " ".join(source.split())
        self.assertRegex(
            source,
            r"\*ES_SYSTIMER_UNIT0_OP\s*=\s*1u\s*<<\s*30;\s*"
            r"while\s*\(!\(\*ES_SYSTIMER_UNIT0_OP\s*&\s*\(1u\s*<<\s*29\)\)\)\s*\{\s*\}\s*"
            r".*?return\s+\*ES_SYSTIMER_UNIT0_LO\s*\*\s*15u;",
        )

    def test_queue_cleanup_requires_successful_open_ownership(self):
        source = (build.HERE / "platform_nuttx.c").read_text()
        self.assertRegex(
            source,
            r"if\s*\(queue_opened\[s\]\[q\]\)\s*\{\s*"
            r"\(void\)mq_close\(queue_handles\[s\]\[q\]\);\s*"
            r"\(void\)mq_unlink\(queue_names\[s\]\[q\]\);\s*"
            r"queue_opened\[s\]\[q\]\s*=\s*0;",
        )
        self.assertRegex(
            source,
            r"(?s)queue_handles\[s\]\[q\]\s*=\s*mq_open\(.*?"
            r"if\s*\(queue_handles\[s\]\[q\]\s*==\s*\(mqd_t\)-1\).*?"
            r"queue_opened\[s\]\[q\]\s*=\s*1;",
        )


if __name__ == "__main__":
    unittest.main()
