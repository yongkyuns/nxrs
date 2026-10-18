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


def default_off_setup_trace(source):
    """Use the real disabled hook when compiling isolated function bodies."""
    header = (source / 'wireless/bluetooth/bt_hcicore.h').read_text()
    return re.search(r'^#\s*define bt_hci_setup_trace\(stage, value\).*$',
                     header, re.M).group() + '\n'


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

    def fixture(self, component):
        marker, patch_dir = SERIES[component]
        upstream = ROOT / "external" / component
        source = self.base / component
        source.mkdir()
        patches = sorted(patch_dir.glob("*.patch"))
        self.assertTrue(patches)
        names = {marker}
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

    def test_bluetooth_le_connection_parameters(self):
        source, _upstream, _patches, _original = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        connection = (source / "wireless/bluetooth/bt_conn.c").read_text()
        header = (source / "wireless/bluetooth/bt_conn.h").read_text()
        core = (source / "wireless/bluetooth/bt_hcicore.c").read_text()
        self.assertIn("hci_le_create_conn(addr, &conn->le_create_param)", core)
        parameters = function_definition(header, "struct bt_le_conn_param_s\n") + ";"
        interval = re.search(r"^\s*uint16_t le_conn_interval;.*$", header, re.M)[0]
        parameters += "\nstruct interval_storage {" + interval + "};"
        definitions = [function_definition(connection, signature) for signature in (
            "static const struct bt_le_conn_param_s g_default_le_param =",
            "static bool le_conn_param_valid(",
            "FAR struct bt_conn_s *bt_conn_add(",
            "FAR struct bt_conn_s *bt_conn_create_le_with_params(",
            "FAR struct bt_conn_s *bt_conn_create_le(",
        )]
        definitions[0] += ";"
        definitions.append(function_definition(core, "static int hci_le_create_conn("))
        definitions.append(function_definition(core, "static void check_pending_conn("))
        harness = (ROOT / "tests/nuttx-std/bt-connection-parameters.c").read_text()
        harness = harness.replace("/* PARAMETERS */", parameters)
        harness = harness.replace("/* FUNCTIONS */", "\n\n".join(definitions))
        program = self.base / "connection-parameters.c"
        binary = self.base / "connection-parameters"
        program.write_text(harness)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        str(program), "-o", str(binary)], check=True, timeout=20)
        subprocess.run([str(binary)], check=True, timeout=10)

    def test_spi_observer_is_opt_in_and_brackets_serialized_transfers(self):
        source, _upstream, _patches, _original = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        transport = (source / "arch/arm/src/rp23xx/rp23xx_cyw43439.c").read_text()
        read = function_definition(transport, "static int my_read(")
        write = function_definition(transport, "static int my_write(")
        self.assertNotIn("rp23xx_cyw_read_", write)
        self.assertLess(read.index("rp23xx_cyw_read_begin("),
                        read.index("rp23xx_gpio_put(rp_io->gpio_select, false)"))
        self.assertLess(read.rindex("rp23xx_pio_sm_set_enabled("),
                        read.index("rp23xx_cyw_read_end("))
        self.assertLess(read.index("rp23xx_cyw_read_end("),
                        read.rindex("nxmutex_unlock("))
        self.assertNotIn("rp23xx_cyw_write_", read)
        self.assertLess(write.index("rp23xx_cyw_write_begin("),
                        write.index("rp23xx_gpio_put(rp_io->gpio_select, false)"))
        self.assertLess(write.rindex("rp23xx_pio_sm_set_enabled("),
                        write.index("rp23xx_cyw_write_end("))
        self.assertLess(write.index("leave_critical_section(flags)"),
                        write.index("rp23xx_cyw_write_end("))
        self.assertLess(write.index("rp23xx_cyw_write_end("),
                        write.rindex("nxmutex_unlock("))
        for enabled in (False, True):
            command = ["cc", "-E", "-P", "-x", "c", "-"]
            if enabled:
                command.insert(1, "-DCONFIG_RP23XX_CYW43439_READ_OBSERVER=1")
            preprocessed = subprocess.run(command, input=read, text=True,
                                          capture_output=True, check=True).stdout
            self.assertEqual(preprocessed.count("rp23xx_cyw_read_begin("), int(enabled))
            self.assertEqual(preprocessed.count("rp23xx_cyw_read_end("), int(enabled))
            command = ["cc", "-E", "-P", "-x", "c", "-"]
            if enabled:
                command.insert(1, "-DCONFIG_RP23XX_CYW43439_WRITE_OBSERVER=1")
            preprocessed = subprocess.run(command, input=write, text=True,
                                          capture_output=True, check=True).stdout
            self.assertEqual(preprocessed.count("rp23xx_cyw_write_begin("), int(enabled))
            self.assertEqual(preprocessed.count("rp23xx_cyw_write_end("), int(enabled))

    def test_bluetooth_link_event_trace_is_numeric_and_default_off(self):
        source, _upstream, _patches, _original = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        core = (source / 'wireless/bluetooth/bt_hcicore.c').read_text()
        header = (ROOT / 'external/nuttx/include/nuttx/wireless/bluetooth/bt_hci.h').read_text()
        structs = []
        for name in ('le_conn_complete', 'disconn_complete'):
            match = re.search(r'begin_packed_struct struct bt_hci_evt_' + name +
                              r'_s\s*\{.*?\} end_packed_struct;', header, re.S)
            self.assertIsNotNone(match)
            structs.append(match.group())
        (self.base / 'bt-link-event-structs.h').write_text('\n'.join(structs))
        for function, output in (('le_conn_complete', 'connect'),
                                  ('hci_disconn_complete', 'disconnect')):
            body = function_definition(core, f'static void {function}(')
            block = re.search(r'#ifdef CONFIG_BLUETOOTH_LINK_TRACE\n.*?#endif', body, re.S)
            self.assertIsNotNone(block)
            (self.base / f'bt-link-event-{output}.h').write_text(block.group())
        for enabled in (False, True):
            command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                       '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                       '-I', str(self.base)]
            if enabled:
                command.append('-DCONFIG_BLUETOOTH_LINK_TRACE=1')
            binary = self.base / ('bt-link-events-' + str(int(enabled)))
            subprocess.run([*command, str(ROOT / 'tests/nuttx-std/bt-link-events.c'),
                            '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_bluetooth_setup_trace_is_bounded_and_hooks_real_stages(self):
        source, _, _, _ = self.fixture('nuttx')
        applied = self.apply('nuttx', source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        core = (source / 'wireless/bluetooth/bt_hcicore.c').read_text()
        header = (source / 'wireless/bluetooth/bt_hcicore.h').read_text()
        stage_enum = re.search(r'enum bt_setup_stage_e\s*\{.*?\};', header, re.S)
        self.assertIsNotNone(stage_enum)
        state = core[core.index('struct bt_setup_record_s\n'):
                     core.index('static void bt_setup_flush(')]
        helpers = '\n'.join(function_definition(core, signature) for signature in (
            'static void bt_setup_flush(', 'void bt_hci_setup_trace(',
            'static void bt_setup_received('))
        send = function_definition(core, 'int bt_send(')
        unit = (stage_enum.group() + '\n#ifdef CONFIG_BLUETOOTH_LINK_TRACE\n' + state +
                helpers + '\n#else\n' + default_off_setup_trace(source) + '#endif\n' + send)
        (self.base / 'bt-setup-stage-functions.h').write_text(unit)
        for enabled in (False, True):
            command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                       '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                       '-I', str(self.base)]
            if enabled:
                command.append('-DCONFIG_BLUETOOTH_LINK_TRACE=1')
            binary = self.base / ('bt-setup-stages-' + str(int(enabled)))
            subprocess.run([*command, str(ROOT / 'tests/nuttx-std/bt-setup-stages.c'),
                            '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
        receive = function_definition(core, 'int bt_receive(')
        self.assertLess(receive.index('bt_setup_received(type, data, len)'),
                        receive.index('bt_buf_alloc('))
        disconnected = function_definition(core, 'static void hci_disconn_complete(')
        self.assertLess(disconnected.index('bt_hci_setup_trace(BT_SETUP_END,'),
                        disconnected.index('bt_conn_lookup_handle('))
        connected = function_definition(core, 'static void le_conn_complete(')
        self.assertGreater(connected.index('bt_hci_setup_trace(BT_SETUP_READY,'),
                           connected.index('bt_connected(conn)'))
        expected = [('bt_conn.c', 'void bt_conn_receive(', 'BT_SETUP_L2CAP_RX', 'bt_l2cap_receive('),
                    ('bt_att.c', 'static void bt_att_receive(', 'BT_SETUP_ATT_RX', 'bt_buf_consume('),
                    ('bt_l2cap.c', 'void bt_l2cap_send(', 'BT_SETUP_ATT_TX', 'bt_buf_provide(')]
        for file, signature, marker, following in expected:
            body = function_definition((source / 'wireless/bluetooth' / file).read_text(), signature)
            self.assertEqual(body.count(marker), 1)
            self.assertLess(body.index(marker), body.index(following))

    def test_wifi_statistics_selects_capability_not_unconditional_counters(self):
        source, upstream, _patches, _original = self.fixture("nuttx")
        result = self.apply("nuttx", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        cases = (
            ("drivers/wireless/ieee80211/bcm43xxx/Kconfig", "IEEE80211_BROADCOM_FULLMAC"),
            ("arch/xtensa/src/common/espressif/Kconfig", "ESPRESSIF_WIFI"),
            ("arch/risc-v/src/common/espressif/Kconfig", "ESPRESSIF_WIFI"),
        )
        for path, symbol in cases:
            with self.subTest(path=path):
                def block(directory):
                    text = (directory / path).read_text()
                    return text.split("config " + symbol + "\n", 1)[1].split("\nconfig ", 1)[0]
                self.assertNotIn("select ARCH_HAVE_NETDEV_STATISTICS", block(upstream))
                patched = block(source)
                self.assertIn("select ARCH_HAVE_NETDEV_STATISTICS", patched)
                self.assertNotRegex(patched, r"select (?:NET_STATISTICS|NETDEV_STATISTICS)\b")

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

    def test_bcmf_frame_trace_is_opt_in_and_bounds_the_actual_collector(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        driver = source / "drivers/wireless/ieee80211/bcm43xxx"
        config = (driver / "Kconfig").read_text().split(
            "config IEEE80211_BROADCOM_FRAME_TRACE\n", 1)[1].split("\nconfig ", 1)[0]
        self.assertIn("default n", config)
        self.assertIn("DEBUG_FEATURES", config)
        text = (driver / "bcmf_bdc.c").read_text()
        start = text.index("void bcmf_bdc_trace_frame(")
        end = text.index("\n}\n", start) + 3
        (self.base / "bcmf-trace-function.h").write_text(text[start:end])
        shutil.copy2(ROOT / "tests/nuttx-std/radio/ffi/stats.c", self.base / "radio-stats.c")
        headers = self.base / "trace-include"
        (headers / "nuttx/net").mkdir(parents=True)
        (headers / "net").mkdir()
        (headers / "nuttx/config.h").write_text(
            "#define CONFIG_NET 1\n#define CONFIG_NET_IPv4 1\n"
            "#define CONFIG_IEEE80211_BROADCOM_FRAME_TRACE 1\n")
        (headers / "nuttx/compiler.h").write_text("#define FAR\n")
        (headers / "nuttx/irq.h").write_text(
            "typedef int irqstate_t;\nirqstate_t enter_critical_section(void);\n"
            "void leave_critical_section(irqstate_t);\n")
        (headers / "nuttx/clock.h").write_text(
            "#define NSEC_PER_TICK 10000000\nuint32_t clock_systime_ticks(void);\n")
        (headers / "nuttx/net/net.h").write_text(
            "int net_lock(void);\nvoid net_unlock(void);\nstruct net_driver_s;\n"
            "struct net_driver_s *netdev_findbyname(const char *);\n")
        (headers / "nuttx/net/netdev.h").write_text(
            "struct net_driver_s { unsigned int d_flags, d_ipaddr; };\n")
        (headers / "net/if.h").write_text(
            "#define IFF_IS_UP(f) (!!((f) & 1))\n#define IFF_IS_RUNNING(f) (!!((f) & 2))\n")
        binary = self.base / "bcmf-trace"
        result = subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-I", str(headers), "-I", str(source / "include"),
            "-I", str(self.base), str(ROOT / "tests/nuttx-std/bcmf-frame-trace.c"),
            "-o", str(binary),
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run([str(binary)], check=True, capture_output=True, text=True)

    def test_bcmf_controller_counters_validate_native_layouts_and_serialize(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        driver = source / "drivers/wireless/ieee80211/bcm43xxx"
        text = (driver / "bcmf_netdev.c").read_text()
        functions = '\n'.join((
            function_definition(text, 'static int bcmf_trace_decode_counts(FAR'),
            function_definition(text, 'int bcmf_trace_counters(FAR'),
        ))
        (self.base / 'bcmf-counters-functions.h').write_text(functions)
        # Native vendor types/offsets, not duplicated host layout stand-ins.
        shutil.copy2(upstream / 'drivers/wireless/ieee80211/bcm43xxx/bcmf_ioctl.h', self.base)
        shutil.copy2(driver / 'bcmf_trace_counters.h', self.base)
        headers = self.base / 'counter-include/nuttx'
        headers.mkdir(parents=True)
        (headers / 'config.h').write_text('#define CONFIG_IEEE80211_BROADCOM_FRAME_TRACE 1\n')
        (headers / 'compiler.h').write_text('#define FAR\n')
        binary = self.base / 'bcmf-counters'
        result = subprocess.run([
            'cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
            '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
            '-I', str(headers.parent), '-I', str(source / 'include'),
            '-I', str(self.base), str(ROOT / 'tests/nuttx-std/bcmf-counters.c'),
            '-o', str(binary),
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run([str(binary)], check=True, capture_output=True, text=True)

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

    def test_bcmf_frame_trace_driver_call_sites(self):
        source, _, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        driver = source / "drivers/wireless/ieee80211/bcm43xxx"


        netdev = (driver / "bcmf_netdev.c").read_text()
        gspi = (driver / "bcmf_gspi_f2_frame.c").read_text()
        worker = (driver / "bcmf_gspi.c").read_text()
        self.assertIn('#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE\n              gbus->trace_status = status;\n              gbus->trace_written = false;\n#endif\n              ret = bcmf_gspi_send_f2_frame(priv);', worker)
        self.assertEqual(worker.count('ret = bcmf_gspi_read_status(priv, &status,'), 2)
        self.assertIn('#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE\n  uint32_t trace_status;', (driver / "bcmf_gspi.h").read_text())
        self.assertIn('#include "cyw_reg_def.h"', gspi)
        self.assertNotIn('#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE\n#include "cyw_reg_def.h"', gspi)
        # Extract the native register constant instead of duplicating its value.
        registers = (ROOT / "external/nuttx/drivers/wireless/ieee80211/bcm43xxx/cyw_reg_def.h").read_text()
        names = ('CYW_REG_STATUS', 'CYW_REG_STATUS_F2_RECEIVE_RDY',
                 'CYW_STATUS_F2_RECEIVE_RDY', 'CYW_STATUS_DATA_NOT_AVAIL',
                 'CYW_STATUS_FIFO_UNDERFLOW', 'CYW_STATUS_FIFO_OVERFLOW',
                 'CYW_STATUS_CMD_DATA_ERROR', 'CYW_STATUS_F2_PKT_AVAIL',
                 'CYW_REG_STATUS_F2_PKT_AVAIL', 'CYW_REG_STATUS_F2_PKT_LEN_MASK',
                 'CYW_REG_STATUS_F2_PKT_LEN_SHIFT', 'CYW_REG_INTERRUPT',
                 'CYW_REG_STATUS_FIFO_UNDERFLOW', 'CYW_REG_STATUS_FIFO_OVERFLOW',
                 'CYW_REG_STATUS_CMD_DATA_ERROR', 'CYW_REG_INTR_ENA',
                 'CYW_REG_INTR_ENA_F2_PKT_AVAIL', 'CYW_REG_INTERRUPT_DATA_NOT_AVAIL',
                 'CYW_REG_INTERRUPT_FIFO_UNDERFLOW', 'CYW_REG_INTERRUPT_FIFO_OVERFLOW',
                 'CYW_REG_INTERRUPT_COMMAND_ERROR', 'CYW_REG_INTERRUPT_DATA_ERROR',
                 'CYW_REG_INTERRUPT_F1_OVERFLOW')
        ready = []
        for name in names:
            lines = re.findall(r"^#define " + name + r"\s+.*$", registers, re.M)
            self.assertEqual(len(lines), 1, name)
            ready.extend(lines)
        error_mask = re.search(r'^#define BCMF_GSPI_ERROR_INTERRUPTS\b.*?(?=\n\n)',
                               worker, re.M | re.S).group()
        ready.append(error_mask)
        public_bus = (source / "include/nuttx/wireless/ieee80211/bcmf_gspi.h").read_text()
        enum = public_bus.split('enum gspi_cmd_func_e', 1)[1].split('};', 1)[0]
        (self.base / "cyw-ready.h").write_text('\n'.join(ready) + '\nenum gspi_cmd_func_e' + enum + '};\n')
        functions = "\n\n".join((
            '#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE\n' +
            function_definition(worker, "static void bcmf_gspi_trace_status(FAR") + '\n#endif',
            function_definition(worker, "static inline int bcmf_gspi_check_result("),
            function_definition(worker, "static inline int bcmf_gspi_read_reg_32(FAR"),
            function_definition(worker, "static inline int bcmf_gspi_read_reg_16(FAR"),
            function_definition(worker, "static inline int bcmf_gspi_write_reg_16(FAR"),
            function_definition(worker, "static int bcmf_gspi_read_status(FAR"),
            function_definition(worker, "static int bcmf_gspi_setup_interrupts(FAR"),
            function_definition(netdev, "static int bcmf_transmit(FAR"),
            function_definition(netdev, "static void bcmf_receive(FAR"),
            function_definition(netdev, "static void bcmf_rxpoll_work(FAR"),
            function_definition(netdev, "void bcmf_netdev_notify_rx(FAR"),
            function_definition(worker, "static int bcmf_gspi_thread_isr("),
            function_definition(gspi, "int bcmf_gspi_read_f2_frame(FAR"),
            function_definition(gspi, "int bcmf_gspi_send_f2_frame(FAR"),
            function_definition(worker, "static int bcmf_gspi_thread(int"),
        ))
        self.assertIn('status_valid = ret == OK;', worker)
        self.assertEqual(worker.count('BCMF_TRACE_DAEMON_WAIT,'), 1)
        self.assertEqual(worker.count('BCMF_TRACE_DAEMON_WAKE,'), 1)
        header = self.base / "bcmf-frame-trace-calls-functions.h"
        header.write_text(functions)
        # Reuse the real observer enum/prototype; mock only platform headers.
        headers = self.base / "callsite-include/nuttx"
        headers.mkdir(parents=True)
        (headers / "config.h").write_text("")
        (headers / "compiler.h").write_text("#define FAR\n")
        # Compile the same extracted definitions twice to prove the config-off
        # bodies contain no observer dependency or call.
        # Remove later fault diagnostics before recreating the pre-0041
        # worker; overlapping loop context is intentionally shared.
        for diagnostic in ('0050-bcmf-hci-ram-readback.patch',
                           '0046-bcmf-hci-bus-fault-readback.patch'):
            reverted = subprocess.run([
                'git', 'apply', '--reverse',
                str(ROOT / 'upstream/nuttx/patches' / diagnostic)],
                cwd=source, env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(source.parent)},
                capture_output=True, text=True)
            self.assertEqual(reverted.returncode, 0, reverted.stderr)
        reverted = subprocess.run([
            'git', 'apply', '--reverse',
            str(ROOT / 'upstream/nuttx/patches/0041-bcmf-gspi-acknowledge-error-interrupts.patch')],
            cwd=source, env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(source.parent)},
            capture_output=True, text=True)
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        old_worker = (driver / 'bcmf_gspi.c').read_text()
        old_functions = functions
        for signature in ('static int bcmf_gspi_thread(int',
                          'static int bcmf_gspi_setup_interrupts(FAR'):
            old_functions = old_functions.replace(function_definition(worker, signature),
                                                  function_definition(old_worker, signature))
        for label, bodies, extra in (
                ('patched', functions, []),
                ('pre-error-irqs', old_functions, ['-DEXPECT_BASELINE_ERROR_IRQS'])):
            header.write_text(bodies)
            for enabled, debug in ((trace, debug) for trace in (True, False)
                                   for debug in (True, False)):
                with self.subTest(trace_enabled=enabled, debug_enabled=debug, worker=label):
                    binary = self.base / f"bcmf-frame-trace-calls-{label}-{enabled}-{debug}"
                    result = subprocess.run([
                        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-Wno-unused-but-set-variable",
                        "-Wno-unused-function", "-fsanitize=address,undefined",
                        "-fno-sanitize-recover=all",
                        "-Wno-sign-compare",
                        "-DCONFIG_IEEE80211_INFINEON_CYW43439_BLUETOOTH=1",
                        *(["-DCONFIG_DEBUG_WIRELESS_ERROR=1"] if debug else []),
                        *extra,
                        *(["-DCONFIG_IEEE80211_BROADCOM_FRAME_TRACE"] if enabled else []),
                        "-I", str(headers.parent), "-I", str(source / "include"),
                        "-I", str(self.base),
                        str(ROOT / "tests/nuttx-std/bcmf-frame-trace-calls.c"),
                        "-o", str(binary),
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run([str(binary)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

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
                default_off_setup_trace(source) + "\n".join(functions))
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
        worker = (source / "drivers/wireless/ieee80211/bcm43xxx/bcmf_gspi.c").read_text()
        # Compile the actual complete implementation with mocked kernel/bus
        # boundaries, rather than duplicating its protocol in the test.
        (self.base / "bcmf-bt-transport.h").write_text(
            "#ifdef CONFIG_DEBUG_WIRELESS_ERROR\n" +
            function_definition(worker, "void bcmf_gspi_bus_diagnostics(") + "\n#endif\n" +
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
            default_off_setup_trace(source) + "\n\n".join(bodies) + "\n")
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
        # observers also touch these functions; reversing 0047 alone is stale.
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
            default_off_setup_trace(source) +
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

    def test_bcmf_auth_event_trace(self):
        source, upstream, _, _ = self.fixture("nuttx")
        applied = self.apply("nuttx", source)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        driver = source / "drivers/wireless/ieee80211/bcm43xxx"

        driver_text = (driver / "bcmf_driver.c").read_text()
        utils_text = (upstream / "drivers/wireless/ieee80211/bcm43xxx/"
                      "bcmf_utils.h").read_text()
        handler = function_definition(
            driver_text, "void bcmf_wl_auth_event_handler(FAR")
        helpers = "\n\n".join((
            function_definition(utils_text, "static inline uint16_t bcmf_getle16("),
            function_definition(utils_text, "static inline uint32_t bcmf_getle32("),
        ))
        (self.base / "bcmf-auth-functions.h").write_text(
            helpers + "\n\n" + handler + "\n")

        bdc_header = (driver / "bcmf_bdc.h").read_text()
        type_start = bdc_header.index("begin_packed_struct struct bcmf_event_s")
        type_end = bdc_header.index("end_packed_struct;", type_start) + len(
            "end_packed_struct;")
        event_type = bdc_header[type_start:type_end]
        event_type = event_type.replace(
            "begin_packed_struct struct", "struct __attribute__((packed))", 1)
        event_type = event_type.replace("end_packed_struct;", ";", 1)
        (self.base / "bcmf-auth-event-type.h").write_text(
            "#include <stdint.h>\n"
            "struct ether_addr { uint8_t octet[6]; };\n"
            + event_type + "\n")

        ioctl_text = (upstream / "drivers/wireless/ieee80211/bcm43xxx/"
                      "bcmf_ioctl.h").read_text()
        native_names = (
            "WLC_E_PSK_SUP", "WLC_E_SET_SSID", "WLC_E_DEAUTH",
            "WLC_E_DEAUTH_IND", "WLC_E_DISASSOC", "WLC_E_DISASSOC_IND",
            "WLC_E_LINK", "WLC_E_STATUS_SUCCESS", "WLC_E_SUP_OTHER",
        )
        constants = ["#include \"bcmf-auth-event-type.h\""]
        offset = re.search(
            r"^#define WLC_E_SUP_REASON_OFFSET\s+\(?\s*(\d+)",
            ioctl_text, re.M)
        self.assertIsNotNone(offset)
        constants.append("#define WLC_E_SUP_REASON_OFFSET " + offset.group(1))
        for name in native_names:
            match = re.search(r"^\s*" + re.escape(name) +
                              r"\s*=\s*([^,\n]+)", ioctl_text, re.M)
            self.assertIsNotNone(match, name)
            expression = re.sub(r"/\*.*?\*/", "", match.group(1)).strip()
            constants.append(f"#define {name} ({expression})")

        if_header = (upstream / "include/net/if.h").read_text()
        for name in ("IFF_RUNNING", "IFF_SET_RUNNING", "IFF_CLR_RUNNING",
                     "IFF_IS_RUNNING"):
            lines = re.findall(r"^#define " + re.escape(name) +
                               r"\b.*$", if_header, re.M)
            self.assertEqual(len(lines), 1, name)
            constants.extend(lines)
        (self.base / "bcmf-auth-native.h").write_text(
            "\n".join(constants) + "\n")

        for enabled in (False, True):
            with self.subTest(trace_enabled=enabled):
                executable = self.base / f"bcmf-auth-event-trace-{enabled}"
                flags = (["-DCONFIG_IEEE80211_BROADCOM_FRAME_TRACE"]
                         if enabled else [])
                compiled = subprocess.run([
                    "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-pedantic", "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=all", *flags,
                    "-I", str(self.base),
                    str(ROOT / "tests/nuttx-std/bcmf-auth-event-trace.c"),
                    "-o", str(executable),
                ], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(executable)], capture_output=True,
                                        text=True, timeout=30)
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
