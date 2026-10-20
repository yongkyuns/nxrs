"""Fail-closed checks for both pinned NuttX upstream patch series."""

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/apply-nuttx-patches.py"
SERIES = {
    "nuttx": ("libs/libc/tls/Kconfig", ROOT / "upstream/nuttx/patches"),
    "nuttx-apps": ("wireless/bluetooth/nimble/Makefile.nimble",
                   ROOT / "upstream/nuttx-apps/patches"),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def function_definition(text, signature):
    begin = text.index(signature)
    while True:
        brace = text.index("{", begin)
        semicolon = text.find(";", begin, brace)
        if semicolon < 0:
            break
        begin = text.index(signature, semicolon)
    depth = 0
    for pos in range(brace, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[begin:pos + 1]
    raise AssertionError(f"unterminated definition: {signature}")


class PatchSeriesTests(unittest.TestCase):
    def test_bluetooth_interface_name_matches_native_adapter_contract(self):
        registration = (ROOT / "external/nuttx/net/netdev/netdev_register.c").read_text()
        self.assertRegex(registration, re.compile(
            r"case NET_LL_BLUETOOTH:.*?devfmt\s*=\s*NETDEV_BNEP_FORMAT;", re.S))
        interface_format = re.search(r'#define NETDEV_BNEP_FORMAT\s+"([^"]+)"', registration)[1]
        self.assertEqual(interface_format.replace("%d", "0"), "bnep0")

    def setUp(self):
        # Archive-like copies nested inside nxrs must not inherit its Git root.
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, component, extra_files=()):
        marker, patch_dir = SERIES[component]
        upstream = ROOT / "external" / component
        source = self.base / component
        source.mkdir()
        patches = sorted(patch_dir.glob("*.patch"))
        self.assertTrue(patches)
        names = {marker, *extra_files}
        for patch in patches:
            names.update(line.split("\t", 2)[2] for line in subprocess.check_output(
                ["git", "apply", "--numstat", str(patch)], text=True,
            ).splitlines())
        original = {}
        for name in names:
            if not (upstream / name).is_file():
                continue  # New files must be created by the series.
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(upstream / name, destination)
            original[name] = digest(destination)
        return source, upstream, patches, original

    def apply(self, component, source):
        return subprocess.run(
            ["python3", str(TOOL), "--component", component,
             "--source", str(source), "--revision", "test-revision",
             "--record", str(self.base / f"{component}-patches.json")],
            text=True, capture_output=True,
        )

    def test_both_series_apply_to_copies_and_record_provenance(self):
        for component in SERIES:
            with self.subTest(component=component):
                source, upstream, patches, original = self.fixture(component)
                result = self.apply(component, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                ledger = json.loads((self.base / f"{component}-patches.json").read_text())
                self.assertEqual(ledger["component"], component)
                self.assertEqual(ledger["upstream_revision"], "test-revision")
                self.assertEqual(len(ledger["patches"]), len(patches))
                latest = {}
                for entry, patch in zip(ledger["patches"], patches):
                    self.assertEqual(entry["name"], patch.name)
                    self.assertEqual(entry["sha256"], digest(patch))
                    for name, hashes in entry["files"].items():
                        self.assertEqual(hashes["before"], latest.get(name, original.get(name)))
                        latest[name] = hashes["after"]
                for name, after in latest.items():
                    self.assertEqual(after, digest(source / name))
                for name, before in original.items():
                    self.assertEqual(digest(upstream / name), before)
                if component == "nuttx":
                    self.assertTrue((source / "arch/xtensa/src/esp32s3/esp32s3_camera.c").is_file())
                    self.assertIn("config TLS_GLOBAL_KEYS", (source / "libs/libc/tls/Kconfig").read_text())
                    self.assertIn("return -EINVAL;", (source / "arch/xtensa/src/esp32s3/esp32s3_spiram.c").read_text())
                    self.assertTrue((source / "boards/xtensa/esp32s3/esp32s3-devkit/src/esp32s3_freenove_userled.c").is_file())
                    transport = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()
                    self.assertIn("#include <nuttx/debug.h>", transport)
                    self.assertIn("set_errno(-err);", transport)
                    self.assertNotIn("set_errno(err);", transport)

    def test_tcp_sequence_rejection_does_not_acknowledge_fin(self):
        source, _, _, _ = self.fixture("nuttx", ("net/tcp/tcp_input.c",))
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        native = (source / "net/tcp/tcp_input.c").read_text()
        begin = min(native.index("  /* Check if the incoming segment acknowledges"),
                    native.index("  /* Check if the sequence number of the incoming packet"))
        end = native.index("  tcp_clear_zero_probe(conn, tcp);", begin)
        close_begin = native.index("      case TCP_LAST_ACK:", end)
        close_end = native.index("      case TCP_CLOSE_WAIT:", close_begin)
        template = (ROOT / "tests/nuttx-std/tcp-input-ack.c").read_text()
        program = self.base / "tcp-input-ack.c"
        program.write_text(template.replace("/* TRIM_FUNCTION */", function_definition(
            native, "static bool tcp_trim_head(")).replace("/* INPUT_SECTION */", native[begin:end])
            .replace("/* CLOSE_STATES */", native[close_begin:close_end]))
        for buffering, sendfile_api in ((False, False), (True, False), (True, True)):
            for fixed_rto in (False, True):
                with self.subTest(buffering=buffering, sendfile_api=sendfile_api,
                                  fixed_rto=fixed_rto):
                    binary = self.base / f"tcp-input-ack-{int(buffering)}-{int(sendfile_api)}-{int(fixed_rto)}"
                    defines = ["-DCONFIG_NET_TCP_WRITE_BUFFERS"] if buffering else []
                    if sendfile_api:
                        defines.append("-DCONFIG_NET_SENDFILE")
                    if fixed_rto:
                        defines.append("-DCONFIG_NET_TCP_FIXED_RTO")
                    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                                    *defines, str(program), "-o", str(binary)],
                                   check=True, timeout=30)
                    subprocess.run([str(binary)], check=True, timeout=10)

    def test_output_dispatch_skips_packets_consumed_by_link_layer(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        native = (source / "net/devif/devif_poll.c").read_text()
        function = function_definition(native, "int devif_poll_out(")
        template = (ROOT / "tests/nuttx-std/netdev-poll-out.c").read_text()
        program = self.base / "netdev-poll-out.c"
        program.write_text(template.replace("/* FUNCTIONS */", function))
        for fragmentation in (False, True):
            with self.subTest(fragmentation=fragmentation):
                binary = self.base / f"netdev-poll-out-{int(fragmentation)}"
                defines = ["-DCONFIG_NET_IPFRAG"] if fragmentation else []
                subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                                *defines, str(program), "-o", str(binary)],
                               check=True, timeout=30)
                subprocess.run([str(binary)], check=True, timeout=10)

    def test_esp32s3_initializes_actual_performance_frequency_before_scheduler(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        start = (source / "arch/xtensa/src/esp32s3/esp32s3_start.c").read_text()
        self.assertRegex(start, r'esp_clk_init\(\);\s*#ifdef CONFIG_ARCH_PERF_EVENTS\s*'
                               r'up_perf_init\(\(void \*\)\(uintptr_t\)esp_clk_cpu_freq\(\)\);\s*#endif')
        self.assertLess(start.index('up_perf_init('), start.index('nx_start();'))
        native = (ROOT / "external/nuttx/arch/xtensa/src/common/xtensa_perf.c").read_text()
        functions = '\n\n'.join(function_definition(native, signature) for signature in (
            'void up_perf_init(', 'unsigned long up_perf_getfreq(', 'void up_perf_convert('))
        template = (ROOT / 'tests/nuttx-std/xtensa-perf-clock.c').read_text()
        program = self.base / 'xtensa-perf-clock.c'
        binary = self.base / 'xtensa-perf-clock'
        program.write_text(template.replace('/* FUNCTIONS */', functions))
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                        str(program), '-o', str(binary)], check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_camera_jpeg_extent_uses_actual_bounded_markers(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        native = (source / "arch/xtensa/src/esp32s3/esp32s3_cam.c").read_text()
        function = function_definition(native, "static uint32_t IRAM_ATTR cam_jpeg_length(")
        gating = function_definition(native, "static void IRAM_ATTR cam_prepare_frame(")
        start = function_definition(native, "static int esp32s3_cam_start_capture(")
        interrupt = function_definition(native, "static int IRAM_ATTR cam_interrupt(")
        set_buffer = function_definition(native, "static int esp32s3_cam_set_buf(")
        self.assertIn("cam_prepare_frame(priv, false);", start)
        self.assertIn("cam_prepare_frame(priv, true);", interrupt)
        template = (ROOT / "tests/nuttx-std/camera-jpeg.c").read_text()
        program = self.base / "camera-jpeg.c"
        binary = self.base / "camera-jpeg"
        program.write_text(template.replace("/* FUNCTION */", function)
                           .replace("/* GATING */", gating)
                           .replace("/* INTERRUPT */", interrupt)
                           .replace("/* SET_BUFFER */", set_buffer))
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        str(program), "-o", str(binary)], check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_ov3660_sensor_lower_half_host_contract(self):
        source, _, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((source / "drivers/video/ov3660.c").is_file())

        # Compile the exact driver from the isolated patched fixture. These
        # host-only headers mock NuttX's API boundary; no Xtensa config is used.
        mock = self.base / "ov3660-mock/include"
        headers = {
            "nuttx/config.h": "",
            "nuttx/arch.h": "",
            "nuttx/kmalloc.h": "",
            "nuttx/i2c/i2c_master.h": r"""
#ifndef TEST_NUTTX_I2C_MASTER_H
#define TEST_NUTTX_I2C_MASTER_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#define I2C_M_READ 0x0001
#define I2C_M_NOSTOP 0x0040
#define I2C_TRANSFER(d, m, n) ((d)->ops->transfer((d), (m), (n)))
struct i2c_master_s;
struct i2c_msg_s { uint32_t frequency; uint16_t addr, flags; uint8_t *buffer; ssize_t length; };
struct i2c_ops_s { int (*transfer)(struct i2c_master_s *, struct i2c_msg_s *, int); };
struct i2c_master_s { const struct i2c_ops_s *ops; };
#endif
""",
            "nuttx/video/video.h": r"""
#ifndef TEST_NUTTX_VIDEO_H
#define TEST_NUTTX_VIDEO_H
#include <stdint.h>
#define V4L2_FRMSIZE_TYPE_DISCRETE 1
#define V4L2_FRMIVAL_TYPE_DISCRETE 1
#define V4L2_PIX_FMT_RGB565X 0x52474251u
#define V4L2_PIX_FMT_JPEG 0x4745504au
struct v4l2_fmtdesc { uint32_t pixelformat; char description[32]; };
struct v4l2_frmsizeenum { uint32_t type; union { struct { uint32_t width, height; } discrete; }; };
struct v4l2_frmivalenum { uint32_t type; union { struct { uint32_t numerator, denominator; } discrete; }; };
#endif
""",
            "nuttx/video/imgsensor.h": r"""
#ifndef TEST_NUTTX_IMGSENSOR_H
#define TEST_NUTTX_IMGSENSOR_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <nuttx/video/video.h>
#define OK 0
#define IMGSENSOR_FMT_MAIN 0
#define IMGSENSOR_PIX_FMT_RGB565X 9
#define IMGSENSOR_PIX_FMT_JPEG 2
#define IMGSENSOR_ID_JPEG_QUALITY 100
#define IMGSENSOR_CTRL_TYPE_INTEGER 1
typedef enum { IMGSENSOR_STREAM_TYPE_VIDEO = 0, IMGSENSOR_STREAM_TYPE_STILL = 1 } imgsensor_stream_type_t;
typedef struct { uint16_t width, height; uint32_t pixelformat; } imgsensor_format_t;
typedef struct { uint32_t numerator, denominator; } imgsensor_interval_t;
typedef struct { int type; union { struct { int64_t minimum, maximum; uint64_t step; int64_t default_value; } range; } u; } imgsensor_supported_value_t;
typedef union { int32_t value32; } imgsensor_value_t;
struct imgsensor_s;
struct imgsensor_ops_s {
  bool (*is_available)(struct imgsensor_s *);
  int (*init)(struct imgsensor_s *);
  int (*uninit)(struct imgsensor_s *);
  const char *(*get_driver_name)(struct imgsensor_s *);
  int (*validate_frame_setting)(struct imgsensor_s *, imgsensor_stream_type_t, uint8_t, imgsensor_format_t *, imgsensor_interval_t *);
  int (*start_capture)(struct imgsensor_s *, imgsensor_stream_type_t, uint8_t, imgsensor_format_t *, imgsensor_interval_t *);
  int (*stop_capture)(struct imgsensor_s *, imgsensor_stream_type_t);
  int (*get_frame_interval)(struct imgsensor_s *, imgsensor_stream_type_t, imgsensor_interval_t *);
  int (*get_supported_value)(struct imgsensor_s *, uint32_t, imgsensor_supported_value_t *);
  int (*get_value)(struct imgsensor_s *, uint32_t, uint32_t, imgsensor_value_t *);
  int (*set_value)(struct imgsensor_s *, uint32_t, uint32_t, imgsensor_value_t);
};
struct imgsensor_s {
  const struct imgsensor_ops_s *ops;
  size_t fmtdescs_num; const struct v4l2_fmtdesc *fmtdescs;
  size_t frmsizes_num; const struct v4l2_frmsizeenum *frmsizes;
  size_t frmintervals_num; const struct v4l2_frmivalenum *frmintervals;
};
#endif
""",
            "nuttx/video/ov3660.h": r"""
#ifndef TEST_NUTTX_OV3660_H
#define TEST_NUTTX_OV3660_H
#include <nuttx/i2c/i2c_master.h>
#include <nuttx/video/imgsensor.h>
struct imgsensor_s *ov3660_initialize(struct i2c_master_s *);
#endif
""",
        }
        for name, content in headers.items():
            path = mock / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        program = ROOT / "tests/nuttx-std/ov3660-image-sensor.c"
        binary = self.base / "ov3660-image-sensor"
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-fsanitize=address,undefined",
                        "-fno-sanitize-recover=all", f"-I{mock}",
                        f"-I{source / 'drivers/video'}", str(program), "-o", str(binary)],
                       check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_scalar_video_controls_initialize_extended_requests(self):
        source, _, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = (source / "drivers/video/v4l2_cap.c").read_text()
        functions = '\n\n'.join(function_definition(capture, signature) for signature in (
            'static int capture_g_ctrl(', 'static int capture_s_ctrl('))
        program = self.base / 'video-controls.c'
        binary = self.base / 'video-controls'
        template = (ROOT / 'tests/nuttx-std/video-controls.c').read_text()
        program.write_text(template.replace('/* FUNCTIONS */', functions))
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                        str(program), '-o', str(binary)], check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_camera_capture_open_close_lifecycle_errors(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        capture = (source / "drivers/video/v4l2_cap.c").read_text()
        functions = "\n\n".join(function_definition(capture, signature) for signature in (
            "static int capture_open(FAR struct file *filep)",
            "static int capture_close(FAR struct file *filep)"))
        template = (ROOT / "tests/nuttx-std/camera-lifecycle.c").read_text()
        self.assertEqual(template.count("/* FUNCTIONS */"), 1)
        program = self.base / "camera-lifecycle.c"
        program.write_text(template.replace("/* FUNCTIONS */", functions))
        binary = self.base / "camera-lifecycle"
        subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wno-unused-parameter", "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all", str(program), "-o", str(binary),
        ], check=True, capture_output=True, timeout=30)
        subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)

    def test_camera_start_errors_propagate_and_preserve_state(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        target = source / "drivers/video/v4l2_cap.c"
        capture = target.read_text()
        framebuff = (source / "drivers/video/video_framebuff.c").read_text()
        framebuff_functions = "\n\n".join(function_definition(framebuff, signature)
            for signature in (
                "vbuf_container_t *video_framebuff_get_container(",
                "void video_framebuff_free_container(",
                "void video_framebuff_queue_container(",
                "vbuf_container_t *video_framebuff_remove_container(",
                "vbuf_container_t *\nvideo_framebuff_get_vacant_container(",
            ))
        for signature in (
                "static int capture_qbuf(FAR struct file *filep,",
                "static int capture_dqbuf(FAR struct file *filep,",
                "static int capture_streamon(FAR struct file *filep,",
                "static int capture_streamoff(FAR struct file *filep,",
                "static int capture_takepict_start(FAR struct file *filep,",
                "static int capture_takepict_stop(FAR struct file *filep,"):
            body = function_definition(capture, signature)
            self.assertIn("return ret;", body, signature)
            self.assertIn("change_capture_state(cmng, next_capture_state)", body,
                          signature)

        template = (ROOT / "tests/nuttx-std/camera-start.c").read_text()
        self.assertEqual(template.count("/* FUNCTIONS */"), 1)
        self.assertEqual(template.count("/* FRAMEBUFF_FUNCTIONS */"), 1)
        env = os.environ.copy()
        env["GIT_CEILING_DIRECTORIES"] = str(source.parent)
        rollback = subprocess.run([
            "git", "apply", "--reverse", "--include=drivers/video/v4l2_cap.c",
            str(ROOT / "upstream/nuttx/patches/0079-video-rollback-failed-qbuf.patch"),
        ], cwd=source, env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        before_rollback_fix = target.read_text()
        for label, native in (("patched", capture), ("before-qbuf-fix", before_rollback_fix)):
            with self.subTest(source=label):
                functions = "\n\n".join(function_definition(native, signature)
                    for signature in (
                        "static int start_capture(FAR struct capture_mng_s *cmng,",
                        "static int change_capture_state(FAR capture_mng_t *cmng,",
                        "static int capture_qbuf(FAR struct file *filep,",
                    ))
                program = self.base / f"camera-start-{label}.c"
                program.write_text(template.replace("/* FUNCTIONS */", functions)
                    .replace("/* FRAMEBUFF_FUNCTIONS */", framebuff_functions))
                binary = self.base / f"camera-start-{label}"
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    str(program), "-o", str(binary),
                ], capture_output=True, text=True, timeout=30)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(binary)], capture_output=True,
                                        text=True, timeout=10)
                if label == "patched":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for marker in ("order=BSD", "unwind=BSs/BSDsd",
                                   "state=preserved", "qbuf=transactional"):
                        self.assertIn(marker, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("failed QBUF returns its container", result.stderr)

    def test_espressif_irq_adapter_lifetime(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        path = "arch/xtensa/src/common/espressif/esp_irq.c"
        header = (upstream / path).with_suffix(".h").read_text()
        adapter = function_definition(header, "struct intr_adapter_from_nuttx\n") + ";"
        template = (ROOT / "tests/nuttx-std/esp-irq-lifetime.c").read_text()
        for label, tree in (("patched", source), ("original", upstream)):
            with self.subTest(source=label):
                native = (tree / path).read_text()
                mapping = re.search(
                    r"static volatile (?:struct esp_irq_entry_s|intr_handle_t)\s+"
                    r"g_handle_map\[[^;]+;", native)
                self.assertIsNotNone(mapping)
                declaration = mapping.group()
                if label == "patched":
                    declaration = (function_definition(native, "struct esp_irq_entry_s\n") +
                                   ";\n" + declaration)
                functions = "\n\n".join(function_definition(native, signature) for signature in (
                    "IRAM_ATTR static int esp_cpuint_to_irq(",
                    "IRAM_ATTR static void isr_adapter_func(",
                    "int esp_set_handle(", "intr_handle_t esp_get_handle(",
                    "int esp_get_irq(", "static void esp_clear_handle(",
                    "int esp_setup_irq_with_flags_intrstatus(", "void esp_teardown_irq(",
                ))
                program = self.base / f"esp-irq-lifetime-{label}.c"
                binary = self.base / f"esp-irq-lifetime-{label}"
                program.write_text(template.replace("/* TYPES */", adapter)
                                   .replace("/* MAP */", declaration)
                                   .replace("/* FUNCTIONS */", functions))
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    str(program), "-o", str(binary),
                ], capture_output=True, text=True, timeout=30)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run(
                    [str(binary), *(["normal-only"] if label == "original" else [])],
                    capture_output=True, text=True, timeout=10)
                if label == "original":
                    self.assertNotEqual(result.returncode, 0, "original adapter leak was not detected")
                    self.assertIn("live_bytes == 0", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("cycles=200 live_bytes=0", result.stdout)

    def test_att_disconnect_resolves_pending_and_detaches_before_callbacks(self):
        source, _, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        att = (source / "wireless/bluetooth/bt_att.c").read_text()
        types = "\n".join(function_definition(att, signature) + ";" for signature in (
            "struct bt_att_req_s\n", "struct bt_att_s\n"))
        signatures = (
            "static void att_req_destroy(", "static uint8_t att_handle_rsp(",
            "static void bt_att_disconnected(", "int bt_att_send(", "void bt_att_cancel(")
        template = (ROOT / "tests/nuttx-std/bt-att-lifetime.c").read_text()
        definitions = "\n\n".join(function_definition(att, signature) for signature in signatures)
        harness = template.replace("/* TYPES */", types).replace("/* FUNCTIONS */", definitions)
        program = self.base / "att-lifetime.c"
        binary = self.base / "att-lifetime"
        program.write_text(harness)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-fsanitize=address,undefined",
                        "-fno-sanitize-recover=all", str(program), "-o", str(binary)],
                       check=True, timeout=20)
        subprocess.run([str(binary)], check=True, timeout=10)

        # Prove the same disconnect witness detects the original bug. Reverse
        # only the new patch in this disposable source copy, never a checkout.
        patch = ROOT / "upstream/nuttx/patches/0059-bluetooth-complete-pending-att-on-disconnect.patch"
        subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=source,
                       env={**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)},
                       check=True, capture_output=True)
        before = (source / "wireless/bluetooth/bt_att.c").read_text()
        definitions = "\n\n".join(function_definition(before, signature) for signature in signatures)
        program.write_text(template.replace("/* TYPES */", types).replace("/* FUNCTIONS */", definitions))
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-fsanitize=address,undefined",
                        "-fno-sanitize-recover=all", str(program), "-o", str(binary)],
                       check=True, timeout=20)
        control = subprocess.run([str(binary), "disconnect-only"], text=True,
                                 capture_output=True, timeout=10)
        self.assertNotEqual(control.returncode, 0)
        self.assertIn("w.calls == 1 && w.destroys == 1 && w.error == BT_ATT_ERR_UNLIKELY", control.stderr)

    def test_gatt_discovery_group_bounds_and_negotiated_mtu(self):
        source, _, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        att = (source / "wireless/bluetooth/bt_att.c").read_text()
        gatt = (source / "wireless/bluetooth/bt_gatt.c").read_text()
        header = (source / "include/nuttx/wireless/bluetooth/bt_gatt.h").read_text()
        parameters = function_definition(header, "struct bt_gatt_discover_params_s\n") + ";"
        definitions = "\n\n".join((
            function_definition(att, "uint16_t bt_att_get_mtu("),
            function_definition(gatt, "uint16_t bt_gatt_get_mtu("),
            function_definition(gatt, "static void att_find_type_rsp("),
        ))
        program = self.base / "gatt-bounds.c"
        program.write_text((ROOT / "tests/nuttx-std/bt-gatt-bounds.c").read_text()
                           .replace("/* PARAMETERS */", parameters)
                           .replace("/* FUNCTIONS */", definitions))
        binary = self.base / "gatt-bounds"
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        str(program), "-o", str(binary)], check=True, timeout=20)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_uuid_cross_width_comparison(self):
        source, _, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        header = (ROOT / "external/nuttx/include/nuttx/wireless/bluetooth/bt_uuid.h").read_text()
        types = "\n".join(function_definition(header, signature) + ";" for signature in (
            "enum bt_uuid_type_e\n", "struct bt_uuid_s\n"))
        template = (ROOT / "tests/nuttx-std/bt-uuid.c").read_text()
        program, binary = self.base / "uuid.c", self.base / "uuid"

        def run():
            uuid = (source / "wireless/bluetooth/bt_uuid.c").read_text()
            base = function_definition(uuid, "static const struct bt_uuid_s g_uuid128_base =") + ";"
            definitions = "\n\n".join(function_definition(uuid, signature) for signature in (
                "static void uuid_to_uuid128(", "static int uuid128_cmp(", "int bt_uuid_cmp("))
            program.write_text(template.replace("/* TYPES */", types)
                               .replace("/* BASE */", base)
                               .replace("/* FUNCTIONS */", definitions))
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                            str(program), "-o", str(binary)], check=True, timeout=20)
            return subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)

        passed = run()
        self.assertEqual(passed.returncode, 0, passed.stderr)
        # The same exhaustive witness must reject the old conversion.
        patch = ROOT / "upstream/nuttx/patches/0063-bluetooth-expand-short-uuid-correctly.patch"
        subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=source,
                       env={**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)},
                       check=True, capture_output=True)
        failed = run()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("memcmp(expanded.u.u128, full_uuid.u.u128, 16) == 0", failed.stderr)

    def test_gatt_discovery_reports_terminal_status(self):
        source, _, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        att = (source / "wireless/bluetooth/bt_att.h").read_text()
        gatt = (source / "wireless/bluetooth/bt_gatt.c").read_text()
        header = (source / "include/nuttx/wireless/bluetooth/bt_gatt.h").read_text()
        parameters = function_definition(header, "struct bt_gatt_discover_params_s\n") + ";"
        wire_types = []
        for signature in (
            "begin_packed_struct struct bt_att_handle_group_s",
            "begin_packed_struct struct bt_att_find_type_rsp_s",
            "begin_packed_struct struct bt_att_data_s",
            "begin_packed_struct struct bt_att_read_type_rsp_s",
            "begin_packed_struct struct bt_att_info_16_s",
            "begin_packed_struct struct bt_att_info_128_s",
            "begin_packed_struct struct bt_att_find_info_rsp_s",
        ):
            start = att.index(signature)
            end = att.index("end_packed_struct;", start) + len("end_packed_struct;")
            wire_types.append(att[start:end].replace("begin_packed_struct", "")
                              .replace("end_packed_struct", "__attribute__((packed))"))
        private_chrc = re.search(
            r"begin_packed_struct struct gatt_chrc_s\s*\{.*?\}\s*end_packed_struct;",
            gatt, re.S).group().replace("begin_packed_struct", "").replace(
                "end_packed_struct", "__attribute__((packed))")
        definitions = "\n\n".join(function_definition(gatt, signature) for signature in (
            "static void att_find_type_rsp(",
            "static void att_read_type_rsp(",
            "static void att_find_info_rsp(",
        ))
        program = self.base / "gatt-discovery-status.c"
        harness = (ROOT / "tests/nuttx-std/bt-gatt-discovery-status.c").read_text()
        harness = (harness.replace("/* PARAMETERS */", parameters)
                   .replace("/* WIRE TYPES */", "\n".join(wire_types))
                   .replace("/* PRIVATE TYPES */", private_chrc)
                   .replace("/* FUNCTIONS */", definitions))
        program.write_text(harness)
        binary = self.base / "gatt-discovery-status"
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-fsanitize=address,undefined",
                        "-fno-sanitize-recover=all", str(program), "-o", str(binary)],
                       check=True, timeout=20)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_bluetooth_scan_failure_recovery(self):
        source, upstream, _, _ = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        template = (ROOT / "tests/nuttx-std/bt-scan-lifetime.c").read_text()
        for name, tree in (("baseline", upstream), ("patched", source)):
            with self.subTest(source=name):
                core = (tree / "wireless/bluetooth/bt_hcicore.c").read_text()
                functions = "\n\n".join(function_definition(core, signature) for signature in (
                    "static int bt_hci_start_scanning(",
                    "static int bt_hci_stop_scanning(",
                    "int bt_start_scanning(", "int bt_stop_scanning(", "int bt_le_scan_update(",
                ))
                program = self.base / f"scan-{name}.c"
                binary = self.base / f"scan-{name}"
                ioctl = (tree / "wireless/bluetooth/bt_ioctl.c").read_text()
                stop = function_definition(ioctl, "case SIOCBTSCANSTOP:")
                program.write_text(template.replace("/* FUNCTIONS */", functions)
                                   .replace("/* IOCTL CASE */", stop))
                subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                                str(program), "-o", str(binary)], check=True, timeout=30)
                result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
                if name == "baseline":
                    self.assertNotEqual(result.returncode, 0, "baseline did not expose the recovery bug")
                    self.assertRegex(result.stderr, r"(?i)assert")
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)


    def test_normal_acl_send_is_not_a_warning(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        path = "wireless/bluetooth/bt_conn.c"
        before = (upstream / path).read_text()
        warning = 'wlwarn("conn handle %u buf len %u\\n", conn->handle, buf->len);'
        self.assertEqual(before.count(warning), 1)
        # Guard the whole send function, not unrelated connection-lifecycle
        # changes elsewhere in the same file.
        self.assertEqual(function_definition((source / path).read_text(), "void bt_conn_send(FAR"),
                         function_definition(before, "void bt_conn_send(FAR").replace(
                             warning, warning.replace("wlwarn", "wlinfo")))

    def test_gatt_connection_callbacks_unregister_quiescently(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        header = (source / "include/nuttx/wireless/bluetooth/bt_gatt.h").read_text()
        text = (source / "wireless/bluetooth/bt_gatt.c").read_text()
        declaration = function_definition(header, "struct bt_gatt_conn_cb_s\n") + ";\n"
        (self.base / "bt-gatt-connection-type.h").write_text(declaration)
        functions = "\n".join(function_definition(text, signature) for signature in (
            "static void bt_gatt_conn_notify(", "int bt_gatt_conn_cb_register(",
            "int bt_gatt_conn_cb_unregister(", "void bt_gatt_connected(",
            "void bt_gatt_disconnected("))
        (self.base / "bt-gatt-connection-functions.h").write_text(functions)
        fixture = (ROOT / "tests/nuttx-std/radio/ffi/gatt.c").read_text()
        globals_text = (function_definition(fixture, "enum nxrs_radio_gatt_state_e\n") + ";\n" +
                        fixture[fixture.index("static pthread_mutex_t g_lock"):
                                fixture.index("static void nxrs_radio_gatt_connected(")])
        (self.base / "bt-gatt-fixture-globals.h").write_text(globals_text)
        fixture_functions = "\n".join(function_definition(fixture, signature) for signature in (
            "static void nxrs_radio_gatt_connected(",
            "static void nxrs_radio_gatt_disconnected(",
            "static struct bt_gatt_conn_cb_s g_conn_cb =")) + ";\n"
        fixture_functions += "\n".join(function_definition(fixture, signature) for signature in (
            "static int nxrs_radio_gatt_value_read(", "static int nxrs_radio_gatt_value_write(",
            "static int nxrs_radio_gatt_ioctl(",
            "int nxrs_radio_gatt_start(", "int nxrs_radio_gatt_snapshot(",
            "int nxrs_radio_gatt_connections(",
            "static int nxrs_radio_gatt_wait_disconnect(", "int nxrs_radio_gatt_stop("))
        (self.base / "bt-gatt-fixture-functions.h").write_text(fixture_functions)
        binary = self.base / "gatt-connection-callbacks"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(self.base), str(ROOT / "tests/nuttx-std/bt-gatt-connection-callbacks.c"),
            "-o", str(binary)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)

    def test_bcmf_frame_capacity_matches_gspi_limit_and_preserves_sdio(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        driver = source / "drivers/wireless/ieee80211/bcm43xxx"
        text = (driver / "bcmf_interface.h").read_text()
        start = text.index("typedef struct bcmf_interface_frame_s")
        end = text.index("} bcmf_interface_frame_t;", start) + len("} bcmf_interface_frame_t;")
        types = text[start:end]
        text = (driver / "bcmf_sdpcm.c").read_text()
        start = text.index("begin_packed_struct struct bcmf_sdpcm_header")
        end = text.index("} end_packed_struct;", start) + len("} end_packed_struct;")
        (self.base / "bcmf-frame-capacity-types.h").write_text(types + "\n" + text[start:end])
        (self.base / "bcmf-frame-capacity-functions.h").write_text("\n".join((
            function_definition(text, "FAR struct bcmf_frame_s *bcmf_sdpcm_alloc_frame("),
            function_definition(text, "void bcmf_sdpcm_free_frame("),
        )))
        for name, defines in (
            ("gspi", ["-DCONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI=1"]),
            ("gspi-unaligned", ["-DCONFIG_IEEE80211_BROADCOM_FULLMAC_GSPI=1",
                                "-DTEST_FRAME_CAPACITY=2049"]),
            ("sdio", ["-DTEST_SDIO=1"]),
        ):
            with self.subTest(bus=name):
                binary = self.base / ("frame-capacity-" + name)
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    *defines, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bcmf-gspi-frame-capacity.c"),
                    "-o", str(binary),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                subprocess.run([str(binary)], check=True, capture_output=True, text=True)

    def test_cyw43439_clm_alignment_matches_both_firmware_resources(self):
        source, _, _, _ = self.fixture("nuttx")
        paths = [source / f"boards/arm/{board}/common/src/{board}_firmware.c"
                 for board in ("rp23xx", "rp2040")]
        baseline = [path.read_text() for path in paths]
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        for path, before in zip(paths, baseline):
            for label, text in (("baseline", before), ("patched", path.read_text())):
                macro = re.search(r"^#define ROUNDED_FIRMWARE_LEN.*$",
                                  text, re.MULTILINE).group(0)
                # Compile the actual board macro: the old Wi-Fi-only resource
                # happened to align at 256 too; WB exposes the wrong CLM offset.
                checks = []
                for length in (224190, 231077, 255, 256, 257, 511, 512, 513):
                    alignment = 256 if label == "baseline" else 512
                    expected = (length + alignment - 1) & ~(alignment - 1)
                    checks.extend([
                        "#undef CONFIG_CYW43439_FIRMWARE_LEN",
                        f"#define CONFIG_CYW43439_FIRMWARE_LEN {length}",
                        f'_Static_assert(ROUNDED_FIRMWARE_LEN == {expected}, "CLM offset");',
                    ])
                witness = self.base / "clm-alignment.c"
                witness.write_text(macro + "\n" + "\n".join(checks) + "\n")
                with self.subTest(board=path.name, firmware=label):
                    result = subprocess.run([
                        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-c",
                        str(witness), "-o", str(self.base / "clm-alignment.o"),
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_pico2w_calibration_is_board_and_radio_config_specific(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)

        firmware = (source / "boards/arm/rp23xx/common/src/rp23xx_firmware.c").read_text()
        initializer_start = firmware.index("const uint8_t g_cyw43439_nvram_image[]")
        initializer_end = firmware.index(";", initializer_start) + 1
        initializer = firmware[initializer_start:initializer_end]
        fallback = re.search(
            r"^#ifndef BOARD_CYW43439_PA2GA0\n.*?^#endif\s*$",
            firmware, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(fallback, "native firmware fallback must remain defined")
        length_match = re.search(
            r"const unsigned int\s+g_cyw43439_nvram_len\s*=\s*sizeof\(g_cyw43439_nvram_image\);",
            firmware,
        )
        self.assertIsNotNone(length_match, "native NVRAM length must track its array")
        board_include = source / "boards/arm/rp23xx/raspberrypi-pico-2/include"
        self.assertTrue((board_include / "board.h").is_file())

        headers = self.base / "calibration-include"
        (headers / "nuttx").mkdir(parents=True)
        (headers / "nuttx/config.h").write_text(
            "#define CONFIG_IEEE80211_INFINEON_CYW43439 1\n")
        board_link = headers / "arch/board/board.h"
        board_link.parent.mkdir(parents=True)
        board_link.symlink_to(board_include / "board.h")
        witness = self.base / "pico2w-calibration.c"
        witness.write_text(
            "#include <stdint.h>\n"
            "#include <stdio.h>\n"
            "#include <arch/board/board.h>\n"
            "#if defined(EXPECT_RADIO_CONFIG)\n"
            "#  if !defined(BOARD_CYW43439_PA2GA0)\n"
            '#    error "Pico 2 W radio config must select board calibration"\n'
            "#  endif\n"
            "#else\n"
            "#  if defined(BOARD_CYW43439_PA2GA0)\n"
            '#    error "Pico 2 W calibration must be absent without radio config"\n'
            "#  endif\n"
            "#endif\n"
            + fallback.group(0) + "\n" + initializer + "\n"
            + length_match.group(0) + "\n"
            "int main(void)\n"
            "{\n"
            "  if (g_cyw43439_nvram_len != sizeof(g_cyw43439_nvram_image)) return 2;\n"
            "  return fwrite(g_cyw43439_nvram_image, 1, g_cyw43439_nvram_len, stdout) "
            "== g_cyw43439_nvram_len ? 0 : 3;\n"
            "}\n"
        )

        images = {}
        for config, defines in (("generic", []),
                                ("pico2w-radio", ["-DEXPECT_RADIO_CONFIG",
                                                  "-DCONFIG_RP23XX_INFINEON_CYW43439=1"])):
            with self.subTest(config=config):
                executable = self.base / f"pico2w-calibration-{config}"
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    *defines, "-I", str(headers), "-I", str(board_include),
                    "-I", str(ROOT / "external/nuttx/boards/arm/rp23xx/raspberrypi-pico-2/include"),
                    str(witness), "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(executable)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                images[config] = result.stdout

        parsed = {}
        for config, image in images.items():
            self.assertTrue(image.endswith(b"\0\0"), config)
            fields = image.rstrip(b"\0").split(b"\0")
            self.assertEqual(len(fields), 46, config)
            self.assertTrue(all(field and b"=" in field for field in fields), config)
            keys = [field.split(b"=", 1)[0] for field in fields]
            self.assertEqual(len(keys), len(set(keys)), config)
            parsed[config] = fields

        generic = parsed["generic"]
        radio = parsed["pico2w-radio"]
        self.assertEqual(len(generic), len(radio))
        differences = [(before, after) for before, after in zip(generic, radio)
                       if before != after]
        self.assertEqual(differences, [(b"pa2ga0=-168,6649,-778",
                                        b"pa2ga0=-168,7161,-820")])

    def test_rejects_reapplication_without_mutation(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        witness = source / "arch/xtensa/src/esp32s3/esp32s3_ble.c"
        before = digest(witness)
        result = self.apply("nuttx", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already applied", result.stderr)
        self.assertEqual(digest(witness), before)

    def test_bcmf_open_network_reset_and_wpa2_staging(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        text = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_driver.c").read_text()
        start = text.index("int bcmf_wl_set_encode_ext(")
        end = text.index("\n/****************************************************************************", start)
        (self.base / "bcmf-function.h").write_text(text[start:end])
        executable = self.base / "bcmf-test"
        compiled = subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror",
                        "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-encoding.c"),
                        "-o", str(executable)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        subprocess.run([str(executable)], check=True, capture_output=True, text=True)

    def test_esp32s3_ble_allocator_pairs_with_separate_and_aliased_heaps(self):
        source, _, _, _ = self.fixture("nuttx")
        adapter = source / "arch/xtensa/src/esp32s3/esp32s3_ble_adapter.c"
        baseline = adapter.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        for label, text in (("baseline", baseline), ("patched", adapter.read_text())):
            functions = []
            names = ["static void *malloc_wrapper(size_t size)",
                     "static void *malloc_internal_wrapper(size_t size)",
                     "static void bt_controller_deinit_internal(void)"]
            if label == "patched":
                names.insert(0, "static void free_wrapper(void *p)")
            for name in names:
                begin = text.index(name + "\n{")
                end = text.index("\n}\n", begin) + 3
                functions.append(text[begin:end])
            # Exercise the real callback-table binding, not a copied choice.
            callback = re.search(r"\._free\s*=\s*(\w+)\s*,", text).group(1)
            functions.append(f"static void (*controller_free)(void *) = {callback};")
            (self.base / "esp32s3-ble-allocator-functions.h").write_text("\n".join(functions))
            for separate in (False, True):
                with self.subTest(adapter=label, separate_heaps=separate):
                    flags = ["-DEXPECT_BASELINE_BUGS"] if label == "baseline" else []
                    if separate:
                        flags.append("-DSEPARATE_HEAPS")
                    executable = self.base / f"esp32s3-allocators-{label}-{separate}"
                    result = subprocess.run([
                        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                        "-I", str(self.base),
                        str(ROOT / "tests/nuttx-std/esp32s3-ble-allocators.c"),
                        "-o", str(executable),
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    subprocess.run([str(executable)], check=True, capture_output=True)

    def test_bluetooth_host_receive_capacity_and_controller_rejection(self):
        source, _, _, _ = self.fixture("nuttx")
        paths = ("include/nuttx/net/bluetooth.h",
                 "include/nuttx/wireless/bluetooth/bt_buf.h",
                 "wireless/bluetooth/bt_hcicore.c",
                 "wireless/bluetooth/bt_buf.c")
        baseline = [(source / path).read_text() for path in paths]
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        patched = [(source / path).read_text() for path in paths]
        for label, texts in (("baseline", baseline), ("patched", patched)):
            frame_header, buffer_header, core, buffers = texts
            capacity = re.search(r"^#define BLUETOOTH_MAX_FRAMELEN.*$",
                                 frame_header, re.MULTILINE).group(0)
            types = [capacity]
            for name in ("bt_buf_hci_data_s", "bt_buf_acl_data_s", "bt_buf_s"):
                begin = buffer_header.index(f"struct {name}\n{{")
                end = buffer_header.index("\n};", begin) + 3
                types.append(buffer_header[begin:end])
            (self.base / "bt-host-buffer-types.h").write_text("\n".join(types))
            functions = []
            for name in ("FAR void *bt_buf_extend(", "FAR void *bt_buf_provide(",
                         "FAR void *bt_buf_consume(", "size_t bt_buf_headroom(",
                         "size_t bt_buf_tailroom("):
                begin = buffers.index(name)
                end = buffers.index("\n}\n", begin) + 3
                functions.append(buffers[begin:end])
            (self.base / "bt-host-buffer-functions.h").write_text("\n".join(functions))
            begin = core.index("  buf = bt_hci_cmd_create(BT_HCI_OP_HOST_BUFFER_SIZE")
            end = core.index("  if (lmp_bredr_capable(g_btdev))", begin)
            initialization = (
                "static int configure_host_buffers(void)\n{\n"
                "  struct bt_buf_s *buf;\n"
                "  struct bt_hci_cp_host_buffer_size_s *hbs;\n"
                "  uint8_t *enable;\n  int ret;\n" + core[begin:end] +
                "  return 0;\n}\n")
            begin = core.index("  if (len", core.index("int bt_receive("))
            end = core.index('  wlinfo("data', begin)
            initialization += ("static int receive_bound(size_t len)\n{\n" +
                               core[begin:end] + "  return 0;\n}\n")
            (self.base / "bt-host-buffer-init.h").write_text(initialization)
            for capacity in (128, 400, 512):
                with self.subTest(host=label, iob_size=capacity):
                    flags = ["-DEXPECT_BASELINE_BUGS"] if label == "baseline" else []
                    executable = self.base / f"bt-host-buffers-{label}-{capacity}"
                    result = subprocess.run([
                        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        f"-DCONFIG_IOB_BUFSIZE={capacity}", *flags,
                        "-I", str(self.base), str(ROOT / "tests/nuttx-std/bt-host-buffers.c"),
                        "-o", str(executable),
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run([str(executable)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_bluetooth_att_mtu_and_command_flag(self):
        source, _, _, _ = self.fixture("nuttx")
        path = source / "wireless/bluetooth/bt_att.c"
        baseline = path.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        for label, text in (("baseline", baseline), ("patched", path.read_text())):
            mask = re.search(r"^#define BT_ATT_OP_CMD_MASK(?:[^\n]*\\\n)*[^\n]*",
                             text, re.MULTILINE).group(0)
            (self.base / "bt-att-mtu-mask.h").write_text(mask + "\n")
            functions = []
            names = ["static void send_err_rsp(", "static uint8_t att_mtu_req(",
                     "static void bt_att_receive("]
            if label == "patched":
                names.insert(0, "uint16_t bt_att_local_mtu(")
            for name in names:
                # Skip prototypes; only a declaration followed by '{' is a body.
                match = re.search(re.escape(name) + r"[^;]+?\n\{", text)
                begin = match.start()
                end = text.index("\n}\n", begin) + 3
                functions.append(text[begin:end])
            (self.base / "bt-att-mtu-functions.h").write_text(
                "\n".join(functions))
            for capacity in (128, 400, 512):
                with self.subTest(att=label, iob_size=capacity):
                    flags = ["-DEXPECT_BASELINE_BUGS"] if label == "baseline" else []
                    executable = self.base / f"bt-att-mtu-{label}-{capacity}"
                    result = subprocess.run([
                        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        f"-DCONFIG_IOB_BUFSIZE={capacity}", *flags,
                        "-I", str(self.base), str(ROOT / "tests/nuttx-std/bt-att-mtu.c"),
                        "-o", str(executable),
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run([str(executable)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_incompatible_source_without_mutation(self):
        source, _, _, _ = self.fixture("nuttx")
        witness = source / "arch/xtensa/src/esp32s3/esp32s3_ble.c"
        witness.write_text(witness.read_text().replace(
            "#include <nuttx/wqueue.h>", "#include <nuttx/different.h>"))
        before = digest(witness)
        result = self.apply("nuttx", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)
        self.assertEqual(digest(witness), before)
        self.assertFalse((self.base / "nuttx-patches.json").exists())

    def test_bcmf_gspi_byte_transfers_preserve_dma_extent(self):
        source, _, _, _ = self.fixture("nuttx")
        driver = source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi.c"
        baseline = driver.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        for label, text in (("baseline", baseline), ("patched", driver.read_text())):
            with self.subTest(transfer=label):
                (self.base / "bcmf-gspi-transfer-function.h").write_text(
                    function_definition(text, "int bcmf_transfer_bytes("))
                binary = self.base / f"bcmf-gspi-transfer-{label}"
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    *(["-Wno-unused-function"] if label == "baseline" else []),
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bcmf-gspi-transfer.c"),
                    "-o", str(binary),
                ], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                if label == "baseline":
                    # Both directions must demonstrate the actual old memory
                    # fault, not merely fail an unrelated assertion.
                    for direction in ("read", "write"):
                        result = subprocess.run([str(binary), direction],
                                                capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("AddressSanitizer: heap-buffer-overflow", result.stderr)
                        self.assertIn("WRITE" if direction == "read" else "READ", result.stderr)
                else:
                    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)

    def test_bcmf_gspi_tx_uses_zero_padded_word_aligned_f2_extent(self):
        source, upstream, _, _ = self.fixture("nuttx")
        patch = ROOT / "upstream/nuttx/patches/0040-bcmf-gspi-align-f2-transfers.patch"
        self.assertTrue(patch.is_file(), f"missing registered patch: {patch}")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)

        relative = Path("drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi_f2_frame.c")
        driver = source / relative
        patched = driver.read_text()
        reverted = subprocess.run(
            ["git", "apply", "--reverse", str(patch)], cwd=source,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)},
            text=True, capture_output=True,
        )
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        baseline = driver.read_text()

        registers = (upstream / "drivers/wireless/ieee80211/bcm43xxx/cyw_reg_def.h").read_text()
        constants = [re.search(r"^#define " + name + r"\s+.*$", registers, re.M).group()
                     for name in ("CYW_REG_STATUS", "CYW_REG_STATUS_F2_RECEIVE_RDY")]
        bus_header = (upstream / "include/nuttx/wireless/ieee80211/bcmf_gspi.h").read_text()
        constants.append(function_definition(bus_header, "enum gspi_cmd_func_e") + ";")
        (self.base / "bcmf-gspi-tx-constants.h").write_text("\n".join(constants) + "\n")

        def compile_witness(label, text, extra=()):
            (self.base / "bcmf-gspi-tx-header.h").write_text(
                function_definition(text, "begin_packed_struct struct f2_frame_header_s")
                + " end_packed_struct;\n"
                + "\ntypedef struct f2_frame_header_s f2_frame_header_t;\n")
            (self.base / "bcmf-gspi-tx-function.h").write_text(
                function_definition(text, "int bcmf_gspi_send_f2_frame("))
            binary = self.base / f"bcmf-gspi-tx-{label}"
            compiled = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                *extra, "-I", str(self.base),
                str(ROOT / "tests/nuttx-std/bcmf-gspi-tx-alignment.c"),
                "-o", str(binary),
            ], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            return binary

        old_binary = compile_witness("pre-0040", baseline,
                                     ("-DEXPECT_PREPATCH_MISMATCH",))
        subprocess.run([str(old_binary)], check=True, capture_output=True, timeout=10)
        fixed_binary = compile_witness("patched", patched)
        subprocess.run([str(fixed_binary)], check=True, capture_output=True, timeout=10)

    def test_bcmf_gspi_receive_buffer_ownership(self):
        source, _, _, _ = self.fixture("nuttx")
        receive = source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi_f2_frame.c"
        baseline = receive.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        patched = receive.read_text()
        framing_patch = ROOT / "upstream/nuttx/patches/0036-bcmf-gspi-validate-receive-frames.patch"
        reverted = subprocess.run(["git", "apply", "--reverse", str(framing_patch)],
                                  cwd=source, env={**os.environ,
                                      "GIT_CEILING_DIRECTORIES": str(source.parent)},
                                  text=True, capture_output=True)
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        pre_framing = receive.read_text()

        def section(text, start):
            begin = text.index(start)
            end = text.index("\n/****************************************************************************", begin)
            return text[begin:end]

        for label, text, flags in (("baseline", baseline, ["-DEXPECT_BASELINE_BUGS"]),
                                   ("pre-framing", pre_framing, ["-DEXPECT_PRE_FRAMING_BUGS"]),
                                   ("patched", patched, [])):
            with self.subTest(receive=label):
                (self.base / "bcmf-gspi-header.h").write_text(
                    section(text, "begin_packed_struct struct f2_frame_header_s"))
                (self.base / "bcmf-gspi-functions.h").write_text(
                    section(text, "int process_f2_frame_header(") + "\n" +
                    section(text, "int bcmf_gspi_read_f2_frame("))
                executable = self.base / f"bcmf-gspi-{label}"
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    "-Wno-sign-compare", *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bcmf-gspi-receive.c"), "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                subprocess.run([str(executable)], check=True, capture_output=True, text=True)

    def test_cyw43439_hci_transport(self):
        source, upstream, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        driver = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi_bt.c").read_text()
        # Compile the actual complete implementation with mocked kernel/bus
        # boundaries, rather than duplicating its protocol in the test.
        (self.base / "bcmf-bt-transport.h").write_text(
            driver[driver.index("#define BCMF_BT_FW_BASE"):])
        netdev = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_netdev.c").read_text()
        begin = netdev.index("static void bcmf_lowpower_ifdown_work(FAR void *arg)\n{")
        end = netdev.index("\n}\n", begin) + 3
        (self.base / "bcmf-bt-idle.h").write_text(netdev[begin:end])
        registers = "\n".join((upstream / "drivers/wireless/ieee80211/bcm43xxx" / name).read_text()
                              for name in ("bcmf_sdio_regs.h", "cyw_reg_def.h"))
        names = ("SBSDIO_FUNC1_SBADDRLOW", "SBSDIO_FUNC1_SBADDRMID", "SBSDIO_FUNC1_SBADDRHIGH",
                 "SBSDIO_SB_OFT_ADDR_MASK", "CYW_REG_RESP_DELAY_F1", "CYW_REG_TEST_RO",
                 "CYW_REG_TEST_RO_PATTERN")
        definitions = "\n".join(re.search(r"^#define " + name + r"\s+.*", registers, re.M)[0]
                                for name in names)
        (self.base / "bcmf-bt-bus-registers.h").write_text(definitions + "\n")
        for label, flags in (("release", []), ("debug", ["-DCONFIG_DEBUG_WIRELESS_ERROR=1"])):
            executable = self.base / f"bcmf-bt-{label}"
            result = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", *flags,
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-gspi-bt.c"),
                "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bluetooth_hci_command_lifetime(self):
        source, _, _, _ = self.fixture("nuttx")
        core = source / "wireless/bluetooth/bt_hcicore.c"
        baseline = core.read_text()
        buffers = source / "wireless/bluetooth/bt_buf.c"
        baseline_buffers = buffers.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        for label, text, flags in (("baseline", baseline, ["-DEXPECT_BASELINE_BUGS"]),
                                   ("patched", core.read_text(), [])):
            with self.subTest(hci=label):
                functions = []
                for name in ("static void hci_cmd_done(", "int bt_hci_cmd_send_sync("):
                    begin = text.index(name)
                    end = text.index("\n}\n", begin) + 3
                    functions.append(text[begin:end])
                (self.base / "bt-hci-command-functions.h").write_text("\n".join(functions))
                buffer_text = baseline_buffers if label == "baseline" else buffers.read_text()
                functions = []
                for name in ("void bt_buf_release(", "FAR struct bt_buf_s *bt_buf_addref("):
                    begin = buffer_text.index(name)
                    end = buffer_text.index("\n}\n", begin) + 3
                    functions.append(buffer_text[begin:end])
                (self.base / "bt-buffer-functions.h").write_text("\n".join(functions))
                executable = self.base / f"bt-hci-command-{label}"
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bt-hci-command.c"), "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(executable)], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bluetooth_advertising_errors(self):
        source, _, _, _ = self.fixture("nuttx")
        ioctl = source / "wireless/bluetooth/bt_ioctl.c"
        core = source / "wireless/bluetooth/bt_hcicore.c"
        baseline_ioctl = ioctl.read_text()
        baseline_core = core.read_text()
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        patched_ioctl = ioctl.read_text()
        patched_core = core.read_text()

        def function(text, signature):
            start = text.index(signature)
            brace = text.index("{", start)
            depth = 0
            for i in range(brace, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
            raise AssertionError(f"unterminated function: {signature}")

        bodies = []
        for label, ioctl_text, core_text in (
                ("baseline", baseline_ioctl, baseline_core),
                ("patched", patched_ioctl, patched_core)):
            start = function(core_text, "int bt_start_advertising(")
            stop = function(core_text, "int bt_stop_advertising(")
            case = ioctl_text.index("      case SIOCBTADVSTOP:")
            brace = ioctl_text.index("{", case)
            depth = 0
            for i in range(brace, len(ioctl_text)):
                if ioctl_text[i] == "{":
                    depth += 1
                elif ioctl_text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        case_body = ioctl_text[case:i + 1]
                        break
            case_body = case_body.replace("bt_stop_advertising()",
                                          f"{label}_stop()")
            bodies.extend((
                start.replace("bt_start_advertising(", f"{label}_start(", 1)
                     .replace("bt_stop_advertising()", f"{label}_stop()"),
                stop.replace("bt_stop_advertising(", f"{label}_stop(", 1),
                f"static int {label}_ioctl_stop(void)\n{{\n"
                f"  int ret = -1;\n  switch (SIOCBTADVSTOP)\n    {{\n"
                f"{case_body}\n        break;\n    }}\n  return ret;\n}}"))
            conn = function(core_text, "static void le_conn_complete(")
            bodies.append(conn.replace(
                "le_conn_complete(", f"{label}_conn_complete(", 1))
        native_hci = (ROOT / "external/nuttx/include/nuttx/wireless/bluetooth/bt_hci.h").read_text()
        roles = re.findall(r"^#\s*define BT_HCI_ROLE_(?:MASTER|SLAVE)\s+0x[0-9a-fA-F]+\s*$",
                           native_hci, re.MULTILINE)
        self.assertEqual(len(roles), 2)
        (self.base / "bt-advertising-roles.h").write_text("\n".join(roles) + "\n")
        (self.base / "bt-advertising-functions.h").write_text(
            "\n\n".join(bodies) + "\n")
        executable = self.base / "bt-advertising"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wno-unused-parameter", "-I", str(self.base),
            str(ROOT / "tests/nuttx-std/bt-advertising.c"), "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bcmf_shared_backplane_window(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        core = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_core.c").read_text()
        functions = []
        for name in ("int bcmf_read_sbreg(", "int bcmf_write_sbreg("):
            begin = core.index(name)
            end = core.index("\n}\n", begin) + 3
            functions.append(core[begin:end])
        (self.base / "bcmf-backplane-functions.h").write_text("\n".join(functions))
        executable = self.base / "bcmf-backplane"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
            "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-backplane.c"),
            "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cyw43439_dma_lifetime(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        path = source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c"
        patched = path.read_text()
        # Reconstruct the pre-fix baseline in reverse series order. Later
        # transfer fixes also touch these functions; reversing 0047 alone is stale.
        ledger = json.loads((self.base / 'nuttx-patches.json').read_text())['patches']
        first = next(i for i,item in enumerate(ledger)
                     if item['name'] == '0047-rp23xx-cyw43439-dma-lifetime.patch')
        for item in reversed(ledger[first:]):
            reverted = subprocess.run([
                'git', 'apply', '--reverse',
                str(ROOT / 'upstream/nuttx/patches' / item['name'])], cwd=source,
                env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(source.parent)},
                capture_output=True, text=True)
            self.assertEqual(reverted.returncode, 0, reverted.stderr)
        types = re.search(r"typedef struct dma_info_s\s*\{.*?\}\s*dma_info_t;",
                          patched, re.S)[0]
        (self.base / "cyw43439-dma-types.h").write_text(types + "\n")
        wait = (upstream / "libs/libc/semaphore/sem_wait.c").read_text()
        (self.base / "cyw43439-dma-wait.h").write_text(
            function_definition(wait, "int nxsem_wait_uninterruptible("))
        dma_header = (upstream / "arch/arm/src/rp23xx/rp23xx_dmac.h").read_text()
        registers = (upstream / "arch/arm/src/rp23xx/hardware/rp23xx_dma.h").read_text()
        begin = dma_header.index("#define RP23XX_DMA_CTRL_BLOCK_END")
        end = dma_header.index("\n\n", begin)
        quiet = re.search(r"^#define RP23XX_DMA_CTRL_TRIG_IRQ_QUIET\s+.*$",
                          registers, re.M)[0]
        (self.base / "cyw43439-dma-end.h").write_text(
            quiet + "\n" + dma_header[begin:end] + "\n")
        for label, code, flags in (("baseline", path.read_text(),
                                   ["-DEXPECT_BASELINE_BUGS", "-fno-sanitize=shift"]),
                                  ("patched", patched, ["-DEXPECT_RX_FIFO_GUARD"]),
                                  ("patched-debug", patched, ["-DEXPECT_RX_FIFO_GUARD",
                                                              "-DCONFIG_DEBUG_WIRELESS_ERROR"])):
            functions = "\n".join(function_definition(code, signature) for signature in (
                "static void dma_complete(", "static int my_read(", "static int my_write("))
            (self.base / "cyw43439-dma-functions.h").write_text(functions)
            executable = self.base / f"cyw43439-dma-lifetime-{label}"
            compiled = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                "-Wno-unused-parameter", "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all", *flags, "-I", str(self.base),
                str(ROOT / "tests/nuttx-std/cyw43439-dma-lifetime.c"), "-o", str(executable)],
                capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bluetooth_disconnect_reason_precedes_observers(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        core = (source / "wireless/bluetooth/bt_hcicore.c").read_text()
        header = (source / "wireless/bluetooth/bt_conn.h").read_text()
        event_header = (upstream / "include/nuttx/wireless/bluetooth/bt_hci.h").read_text()
        field = re.search(r"^\s*uint8_t disconn_reason;.*$", header, re.M)[0]
        event = re.search(r"begin_packed_struct struct bt_hci_evt_disconn_complete_s\s*"
                          r"\{.*?end_packed_struct;", event_header, re.S)[0]
        (self.base / "bt-disconnect-field.h").write_text(field + "\n")
        (self.base / "bt-disconnect-event.h").write_text(event + "\n")
        (self.base / "bt-disconnect-functions.h").write_text(
            function_definition(core, "static void hci_disconn_complete("))
        binary = self.base / "bt-disconnect-reason"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(self.base), str(ROOT / "tests/nuttx-std/bt-disconnect-reason.c"),
            "-o", str(binary)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rp23xx_pio_dreq_mapping(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        header = "arch/arm/src/rp23xx/rp23xx_pio.h"
        constants = "\n".join((upstream / "arch/arm/src/rp23xx/hardware" / name).read_text()
                              for name in ("rp23xx_pio.h", "rp23xx_dreq.h"))
        constants += "\n" + (upstream / header).read_text()
        definitions = "\n".join(re.search(r"^#define " + name + r"\s+.*", constants, re.M)[0]
                                for name in ("RP23XX_PIO_SM_NUM", "RP23XX_DMA_DREQ_PIO0_TX0",
                                             "RP23XX_DMA_DREQ_PIO1_TX0", "RP23XX_DMA_DREQ_PIO2_TX0"))
        for label, directory, flags in (("baseline", upstream, ["-DEXPECT_BASELINE_BUG"]),
                                        ("patched", source, [])):
            code = (directory / header).read_text()
            (self.base / "rp23xx-pio-dreq.h").write_text(definitions + "\n" +
                function_definition(code, "static inline uint32_t rp23xx_pio_get_dreq("))
            executable = self.base / f"rp23xx-pio-dreq-{label}"
            compiled = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(self.base), str(ROOT / "tests/nuttx-std/rp23xx-pio-dreq.c"),
                "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_rp23xx_dma_status_acknowledges_owned_mask(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        path = "arch/arm/src/rp23xx/rp23xx_dmac.c"
        dma_header = (upstream / "arch/arm/src/rp23xx/rp23xx_dmac.h").read_text()
        registers = (upstream / "arch/arm/src/rp23xx/hardware/rp23xx_dma.h").read_text()
        # Relocate real register offsets to a harmless mock address space.
        definitions = "#define RP23XX_DMA_BASE 0x50000000u\n"
        definitions += "\n".join(re.findall(r"^#define RP23XX_DMA_.*", registers, re.M))
        definitions += "\n" + re.search(r"^#define RP23XX_DMA_NCHANNELS.*", dma_header, re.M)[0]
        definitions += "\n" + re.search(r"typedef [^;]*DMA_HANDLE;", dma_header)[0]
        definitions += "\n" + re.search(r"typedef void \(\*dma_callback_t\)[^;]*;", dma_header)[0]
        memory_map = (upstream / "arch/arm/src/rp23xx/hardware/rp23xx_memorymap.h").read_text()
        for name in ("RP23XX_ATOMIC_SET_REG_OFFSET", "RP23XX_ATOMIC_CLR_REG_OFFSET",
                     "setbits_reg32", "clrbits_reg32"):
            definitions += "\n" + re.search(r"^#\s*define " + name + r"(?:\s|\().*",
                                             memory_map, re.M)[0]
        (self.base / "rp23xx-dma-definitions.h").write_text(definitions + "\n")
        for label, directory, flags in (("baseline", upstream, ["-DEXPECT_BASELINE_ALIAS"]),
                                        ("patched", source, [])):
            code = (directory / path).read_text()
            functions = "\n".join(function_definition(code, signature) for signature in (
                "DMA_HANDLE rp23xx_dmachannel(", "void rp23xx_dmafree("))
            (self.base / "rp23xx-dma-functions.h").write_text(functions)
            executable = self.base / f"rp23xx-dma-status-{label}"
            compiled = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(self.base), str(ROOT / "tests/nuttx-std/rp23xx-dma-status.c"),
                "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cyw43439_pio_rx_count(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        path = source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c"
        patched = path.read_text()
        result = subprocess.run(
            ["git", "apply", "--reverse", str(ROOT / "upstream/nuttx/patches/"
                                             "0044-rp23xx-cyw43439-pio-rx-count.patch")],
            cwd=source, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for label, code, flags in (("baseline", path.read_text(), ["-DEXPECT_BASELINE_BUG"]),
                                   ("patched", patched, [])):
            begin = code.index("  bit_length =", code.index("static int my_read("))
            end = code.index("  rp23xx_pio_sm_put(rp_io->pio, rp_io->pio_sm, bit_length);", begin)
            end += len("  rp23xx_pio_sm_put(rp_io->pio, rp_io->pio_sm, bit_length);")
            (self.base / "cyw43439-pio-count-setup.h").write_text(code[begin:end])
            begin = code.index("static const uint16_t cyw_program_instructions[]")
            end = code.index("\n};", begin) + 3
            (self.base / "cyw43439-pio-program.h").write_text(code[begin:end])
            executable = self.base / f"cyw-pio-count-{label}"
            result = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(self.base), str(ROOT / "tests/nuttx-std/cyw43439-pio-count.c"),
                "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cyw43439_irq_state(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        text = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()
        reverse = subprocess.run(
            ["git", "apply", "--reverse",
             str(ROOT / "upstream/nuttx/patches/0015-rp23xx-cyw43439-irq-state.patch")],
            cwd=source, env={**os.environ,
                           "GIT_CEILING_DIRECTORIES": str(source.parent)},
            capture_output=True, text=True,
        )
        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        original = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()

        def function(code, name):
            begin = code.index(name)
            end = code.index("\n}\n", begin) + 3
            return code[begin:end]

        for label, code, flags in (("baseline", original, ["-DEXPECT_BASELINE_BUG"]),
                                    ("patched", text, [])):
            functions = function(code, "static int my_interrupt_enable(")
            if label == "patched":
                functions += "\n" + function(code, "static void my_transfer_irq(")
            (self.base / "cyw43439-irq-functions.h").write_text(functions)
            executable = self.base / f"cyw-irq-{label}"
            compiled = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                "-I", str(self.base), str(ROOT / "tests/nuttx-std/cyw43439-irq.c"),
                "-o", str(executable),
            ], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cdcacm_bootloader_trigger(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        driver = (source / "drivers/usbdev/cdcacm.c").read_text()
        board = (source / "boards/arm/rp23xx/common/src/rp23xx_reset.c").read_text()

        def function(code, name):
            begin = code.index(name)
            return code[begin:code.index("\n}\n", begin) + 3]

        begin = driver.index("        case ACM_SET_LINE_CODING:")
        end = driver.index("        /* ACM_SET_CTRL_LINE_STATE:", begin)
        (self.base / "cdcacm-linecoding.h").write_text(driver[begin:end])
        (self.base / "cdcacm-reset.h").write_text(
            function(driver, "static void cdcacm_bootloader("))
        (self.base / "cdcacm-rom.h").write_text(
            function(board, "static void rp23xx_bootloader(") + "\n" +
            function(board, "void board_usbdev_bootloader(") + "\n" +
            function(board, "int board_reset("))
        for name in ("cdcacm_unbind(", "cdcuart_release("):
            # Driver unbind and final serial release cancel embedded timers.
            definition = driver.index(name, driver.index("static void cdcacm_unbind(") + 1)
            self.assertIn("wd_cancel(&priv->bootloader);",
                          driver[definition:driver.index("\n}\n", definition)])
        configurations = (
            ("default", []),
            ("trigger-default-rom-policy", ["-DCONFIG_CDCACM_1200BPS_BOOTLOADER"]),
            ("headless-msd-disabled", ["-DCONFIG_RP23XX_USB_BOOTSEL_DISABLE_MSD"]),
            ("trigger-msd-disabled", ["-DCONFIG_CDCACM_1200BPS_BOOTLOADER",
                                      "-DCONFIG_RP23XX_USB_BOOTSEL_DISABLE_MSD"]),
        )
        for name, flags in configurations:
            with self.subTest(configuration=name):
                executable = self.base / f"cdcacm-reset-{name}"
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    "-I", str(self.base), str(ROOT / "tests/nuttx-std/cdcacm-bootloader.c"),
                    "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rp23xx_usb_completion_rearm(self):
        source, upstream, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        usb_path = "arch/arm/src/rp23xx/rp23xx_usbdev.c"
        fixture_usb = source / usb_path
        fixture_usb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(upstream / usb_path, fixture_usb)
        driver = fixture_usb.read_text()

        def function(name):
            start = driver.rindex(name)
            end = driver.index("\n}\n", start) + 3
            return driver[start:end]

        definitions = "\n\n".join(function(signature) for signature in (
            "static void rp23xx_update_buffer_control(",
            "static void rp23xx_txcomplete(",
            "static void rp23xx_rxcomplete(",
        ))
        (self.base / "rp23xx-usb-functions.h").write_text(definitions)
        executable = self.base / "rp23xx-usb-rearm"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(self.base),
            str(ROOT / "tests/nuttx-std/rp23xx-usb-rearm.c"),
            "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tcp_poll_connecting_sockets(self):
        source, _, _, _ = self.fixture("nuttx")
        path = "net/tcp/tcp_netpoll.c"
        baseline = (source / path).read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        patched = (source / path).read_text()

        def function(text, signature):
            start = text.index(signature)
            brace = text.index("{", start)
            depth = 0
            for i in range(brace, len(text)):
                if text[i] == "{": depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0: return text[start:i + 1]
            raise AssertionError(f"unterminated function: {signature}")

        bodies = []
        for text, prefix in ((baseline, "baseline"), (patched, "patched")):
            event = function(text, "static uint32_t tcp_poll_eventhandler(")
            setup = function(text, "int tcp_pollsetup(FAR struct socket *psock")
            for body, name in ((event, "tcp_poll_eventhandler"),
                               (setup, "tcp_pollsetup")):
                renamed = f"{prefix}_{'event' if name.endswith('eventhandler') else 'setup'}"
                body = body.replace(name, renamed, 1)
                if name == "tcp_pollsetup":
                    body = body.replace("tcp_poll_eventhandler", f"{prefix}_event")
                bodies.append(body)
        (self.base / "tcp-poll-functions.h").write_text("\n\n".join(bodies) + "\n")

        for buffered in (False, True):
            with self.subTest(write_buffers=buffered):
                executable = self.base / f"tcp-connect-poll-{buffered}"
                flags = ["-DCONFIG_NET_TCP_WRITE_BUFFERS"] if buffered else []
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-pedantic",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/tcp-connect-poll.c"),
                    "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_tcp_retransmission_progress_uses_actual_patched_functions(self):
        source, upstream, _patches, _original = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        patch = ROOT / "upstream/nuttx/patches/0042-tcp-preserve-retransmission-progress.patch"

        targets = (
            ("net/tcp/tcp_send.c", "static void tcp_sendcommon(", "tcp_sendcommon"),
            ("net/tcp/tcp_appsend.c", "void tcp_appsend(", "tcp_appsend"),
            ("net/tcp/tcp_timer.c", "void tcp_update_retrantimer(", "tcp_update_retrantimer"),
        )
        patched_bodies = {}
        for path, signature, name in targets:
            patched_bodies[name] = function_definition((source / path).read_text(), signature)

        # Isolate exactly 0042 from the already-applied complete patch series.
        reversed_patch = subprocess.run(
            ["git", "apply", "--reverse", str(patch)], cwd=source,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)},
            text=True, capture_output=True,
        )
        self.assertEqual(reversed_patch.returncode, 0, reversed_patch.stderr)
        baseline_bodies = {
            name: function_definition((source / path).read_text(), signature)
            for path, signature, name in targets
        }
        native_constants = (upstream / 'include/nuttx/net/tcp.h').read_text()
        native_constants += '\n' + (upstream / 'net/devif/devif.h').read_text()
        names = ('TCP_STOPPED', 'TCP_CLOSED', 'TCP_SYN', 'TCP_FIN', 'TCP_RST',
                 'TCP_ACK', 'TCP_SNDACK', 'TCP_TXCLOSE', 'TCP_ABORT', 'NETDEV_DOWN',
                 'TCP_CLOSE_WAIT', 'TCP_LAST_ACK', 'TCP_FIN_WAIT_1', 'TCP_POLL')
        constants = []
        for name in names:
            definition = re.search(r'^#\s*define\s+' + name + r'\s+[^\n]*',
                                   native_constants, re.M)
            self.assertIsNotNone(definition, name)
            constants.append(definition.group())
        (self.base / 'tcp-retransmission-constants.h').write_text('\n'.join(constants) + '\n')

        generated = []
        for version, bodies in (("patched", patched_bodies), ("baseline", baseline_bodies)):
            for name, body in bodies.items():
                body = body.replace(name, f"{name}_{version}", 1)
                if name == "tcp_sendcommon":
                    body = body.replace("tcp_update_retrantimer(",
                                        f"tcp_update_retrantimer_{version}(")
                generated.append(body)
        (self.base / "tcp-retransmission-functions.h").write_text("\n\n".join(generated) + "\n")

        for mode, flags in (
                ('unbuffered', []),
                ('buffered', ['-DCONFIG_NET_TCP_WRITE_BUFFERS']),
                ('sendfile', ['-DCONFIG_NET_TCP_WRITE_BUFFERS', '-DCONFIG_NET_SENDFILE'])):
            with self.subTest(sender=mode):
                executable = self.base / ('tcp-retransmission-progress-' + mode)
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/tcp-retransmission-progress.c"),
                    "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_dhcpc_pad_preserves_message_type_and_lease_options(self):
        source, upstream, _, _ = self.fixture("nuttx-apps")
        applied = self.apply("nuttx-apps", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        config = self.base / "nuttx/config.h"
        config.parent.mkdir()
        config.write_text("#define FAR\n#define CONFIG_NETDB_DNSSERVER_NAMESERVERS 2\n"
                          "#define CONFIG_NETUTILS_DHCPC_NTP_SERVERS 2\n")

        def build(label):
            native = (source / "netutils/dhcpc/dhcpc.c").read_text()
            definitions = "\n".join(re.findall(r"^#define DHCP_OPTION_.*$", native, re.M))
            parser = function_definition(native, "static uint8_t dhcpc_parseoptions(")
            template = (ROOT / "tests/nuttx-std/dhcpc-pad-option.c").read_text()
            program = self.base / (label + ".c")
            program.write_text(template.replace("/* DEFINITIONS */", definitions)
                               .replace("/* PARSER */", parser))
            binary = self.base / label
            subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", str(self.base), "-I", str(upstream / "include"),
                str(program), "-o", str(binary),
            ], check=True, capture_output=True, timeout=30)
            return binary

        passed = subprocess.run([build("dhcpc-pad-patched")], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertIn("checks=98 single_byte_boundaries=3", passed.stdout)
        patch = ROOT / "upstream/nuttx-apps/patches/0003-dhcpc-pad-option.patch"
        subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=source,
                       env={**os.environ, "GIT_CEILING_DIRECTORIES": str(source.parent)},
                       check=True, capture_output=True, timeout=10)
        original = build("dhcpc-pad-original")
        subprocess.run([original, "no-pad-controls"], check=True, timeout=10)
        failed = subprocess.run([original], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("dhcpc_parseoptions(&state, data, length) == type", failed.stderr)

    def test_nsh_dmesg_resets_getopt_and_preserves_clear_semantics(self):
        source, upstream, _, _ = self.fixture("nuttx-apps")
        path = "nshlib/nsh_fscmds.c"
        original = function_definition((upstream / path).read_text(), "int cmd_dmesg(")
        applied = self.apply("nuttx-apps", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        patched = function_definition((source / path).read_text(), "int cmd_dmesg(")
        self.assertIn('"[-c] read and clear, [-C] clear only"',
                      (source / "nshlib/nsh_command.c").read_text())

        # Compile the pinned parser, not the host libc or a cursor simulation.
        nuttx = ROOT / "external/nuttx"
        unistd = nuttx / "libs/libc/unistd"
        common = (unistd / "lib_getopt_common.c").read_text()
        functions = [function_definition(common, signature) for signature in (
            "static int compare_long_option(", "static int getopt_long_option(",
            "int getopt_common(")]
        functions.append(function_definition((unistd / "lib_getopt.c").read_text(),
                                             "int getopt("))
        (self.base / "nsh-getopt-functions.h").write_text("\n\n".join(functions) + "\n")
        types = []
        for path, signature in (
            ("include/nuttx/tls.h", "struct getopt_s\n"),
            ("include/getopt.h", "struct option\n"),
            ("libs/libc/unistd/unistd.h", "enum getopt_mode_e\n"),
        ):
            types.append(function_definition((nuttx / path).read_text(), signature) + ";")
        for path, names in (
            ("include/getopt.h", ("no_argument", "required_argument", "optional_argument")),
            ("libs/libc/unistd/unistd.h", ("GETOPT_LONG_BIT", "GETOPT_LONGONLY_BIT",
                                         "GETOPT_HAVE_LONG", "GETOPT_HAVE_LONGONLY")),
        ):
            header = (nuttx / path).read_text()
            definitions = [re.search(r"^#define " + name + r"\b.*$", header, re.M).group()
                           for name in names]
            types[0:0] = definitions
        (self.base / "nsh-getopt-types.h").write_text("\n".join(types) + "\n")

        for label, body in (("baseline", original), ("patched", patched)):
            with self.subTest(implementation=label):
                (self.base / "nsh-dmesg-function.h").write_text(body + "\n")
                binary = self.base / ("nsh-dmesg-" + label)
                flags = ["-DEXPECT_BASELINE"] if label == "baseline" else []
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-pedantic", "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=all", *flags, "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/nsh-dmesg.c"), "-o", str(binary),
                ], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bluetooth_connection_reclaims_controller_credits(self):
        source, upstream, _, _ = self.fixture("nuttx")
        connection = source / "wireless/bluetooth/bt_conn.c"
        core = source / "wireless/bluetooth/bt_hcicore.c"
        baseline_connection = connection.read_text()
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        native = (upstream / "include/nuttx/wireless/bluetooth/bt_hci.h").read_text()
        types = []
        for name in ("bt_hci_handle_count_s", "bt_hci_evt_num_completed_packets_s"):
            begin = native.index("begin_packed_struct struct " + name + "\n")
            end = native.index("end_packed_struct;", begin) + len("end_packed_struct;")
            types.append(native[begin:end].replace("begin_packed_struct struct",
                         "struct __attribute__((packed))").replace("end_packed_struct;", ";"))
        (self.base / "bt-connection-credit-types.h").write_text("\n".join(types))
        # Isolate the disconnect gap: baseline uses no completion event; the
        # patched case also exercises the length-qualified native dispatcher.
        dispatch = function_definition(core.read_text(), "static void hci_num_completed_packets(")
        for label, text in (("baseline", baseline_connection),
                            ("patched", connection.read_text())):
            with self.subTest(credits=label):
                helpers = [] if label == "baseline" else [
                    function_definition(text, signature) for signature in (
                        "static void bt_conn_return_credits(",
                        "static bool bt_conn_reserve_tx(",
                        "void bt_conn_tx_complete(")]
                helpers.append(function_definition(text, "void bt_conn_set_state("))
                if label == "patched":
                    helpers.append(dispatch)
                    helpers.append(function_definition(text, "static int conn_tx_kthread(").replace(
                        "static int conn_tx_kthread(", "static int actual_conn_tx_kthread(", 1))
                (self.base / "bt-connection-credit-functions.h").write_text("\n\n".join(helpers))
                binary = self.base / f"bt-connection-credits-{label}"
                result = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-function", "-Wno-unused-parameter",
                    "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=all",
                    *(["-DEXPECT_BASELINE_CREDIT_LEAK"] if label == "baseline" else []),
                    "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bt-connection-credits.c"), "-o", str(binary),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)

    def test_bcmf_gspi_register_and_power_errors(self):
        source, _, _, _ = self.fixture("nuttx")
        self.assertEqual(self.apply("nuttx", source).returncode, 0)
        driver = source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi.c"
        text = driver.read_text()
        signatures = (
            "static inline int bcmf_gspi_check_result(",
            "static inline int bcmf_gspi_read_reg_32(FAR",
            "static inline int bcmf_gspi_read_reg_16(FAR",
            "static inline int bcmf_gspi_read_reg_8(FAR",
            "static inline int bcmf_gspi_write_reg_16(FAR",
            "static inline int bcmf_gspi_write_reg_8(FAR",
            "static int bcmf_gspi_kso_enable(FAR",
            "static int bcmf_gspi_bus_sleep(FAR",
            "static int bcmf_gspi_setup_interrupts(FAR",
            "static int bcmf_gspi_init_alp_clock(FAR",
            "static int bcmf_gspi_init_device(FAR",
            "int bcmf_read_reg(FAR",
            "int bcmf_write_reg(FAR",
        )
        functions = "\n\n".join(function_definition(text, signature)
                                 for signature in signatures)
        functions = re.search(
            r'^#define BCMF_GSPI_ERROR_INTERRUPTS\b.*?(?=\n\n)',
            text, re.M | re.S).group() + '\n\n' + functions
        (self.base / "bcmf-gspi-register-functions.h").write_text(functions)

        gspi_header = (source / "include/nuttx/wireless/ieee80211/"
                       "bcmf_gspi.h").read_text()
        enum_start = gspi_header.index("enum gspi_cmd_func_e")
        enum_end = gspi_header.index("};", enum_start) + 2
        enum = gspi_header[enum_start:enum_end]
        native_driver = (ROOT / "external/nuttx/drivers/wireless/ieee80211/"
                         "bcm43xxx")
        sdio_header = (native_driver / "bcmf_sdio_regs.h").read_text()
        cyw_header = (native_driver / "cyw_reg_def.h").read_text()
        macro_sources = (sdio_header, cyw_header)
        definitions = {}
        for header in macro_sources:
            for match in re.finditer(
                    r"^#define\s+([A-Za-z_]\w*)\b[^\n]*(?:\\\n[^\n]*)*",
                    header, re.M):
                definitions[match.group(1)] = match.group(0)
        needed = set(re.findall(r"\b(?:CYW|SBSDIO)_[A-Z0-9_]+\b", functions))
        pending = list(needed)
        while pending:
            name = pending.pop()
            self.assertIn(name, definitions, f"missing native definition: {name}")
            dependencies = set(re.findall(
                r"\b(?:CYW|SBSDIO)_[A-Z0-9_]+\b", definitions[name])) - needed
            needed.update(dependencies)
            pending.extend(dependencies)
        rev16 = re.search(r"^#define REV16\(x\).*?(?=\n\n)", text, re.M | re.S)
        self.assertIsNotNone(rev16)
        ready_tries = re.search(r"^#define BCMF_GSPI_READY_TRYS\s+.*$", text, re.M)
        self.assertIsNotNone(ready_tries)
        constants = "\n".join(definitions[name] for name in sorted(needed))
        (self.base / "bcmf-gspi-register-constants.h").write_text(
            enum + "\n" + rev16.group(0) + "\n" + ready_tries.group(0) +
            "\n" + constants + "\n")

        executable = self.base / "bcmf-gspi-registers"
        compiled = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I", str(self.base),
            str(ROOT / "tests/nuttx-std/bcmf-gspi-registers.c"),
            "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(executable)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
