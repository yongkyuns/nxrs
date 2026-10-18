"""Address-policy comparison uses HCI byte order, without logging addresses."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('identity_extract', ROOT / 'tests/nuttx-std/test-patches.py')
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


class IdentityTests(unittest.TestCase):
    def test_native_comparison_matches_sdk_last_octet_policy(self):
        source = (ROOT / 'tests/nuttx-std/radio/ffi/gatt.c').read_text()
        function = extract.function_definition(source, 'static int nxrs_radio_address_matches_sdk(')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            program = directory / 'identity.c'
            program.write_text('#include <assert.h>\n#include <stdint.h>\n#include <string.h>\n' +
                               function + r'''
int main(void) {
    uint8_t wifi[6] = {0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc};
    uint8_t bluetooth[6] = {0xbd, 0x9a, 0x78, 0x56, 0x34, 0x12};
    assert(nxrs_radio_address_matches_sdk(bluetooth, wifi));
    for (unsigned i = 0; i < 6; ++i) {
        bluetooth[i] ^= 1;
        assert(!nxrs_radio_address_matches_sdk(bluetooth, wifi));
        bluetooth[i] ^= 1;
    }
    assert(!nxrs_radio_address_matches_sdk(wifi, bluetooth));
    wifi[5] = 0xff;
    bluetooth[0] = 0;
    assert(nxrs_radio_address_matches_sdk(bluetooth, wifi));
    bluetooth[1]++;
    assert(!nxrs_radio_address_matches_sdk(bluetooth, wifi));
    return 0;
}
''')
            binary = directory / 'identity'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                            str(program), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
