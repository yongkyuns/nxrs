"""Independent oracle and boundary coverage for native cast qualification."""
import importlib.util
from pathlib import Path
import unittest

import conversions

SPEC = importlib.util.spec_from_file_location('fp_casts', Path(__file__).parent/'compiler/check_fp_casts.py')
casts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(casts)


class NativeCastVectorsTests(unittest.TestCase):
    def test_every_exponent_both_signs_and_extreme_fractions_are_covered(self):
        for source, fraction_bits, max_exponent in ((32, 23, 255), (64, 52, 2047)):
            rows = casts.inputs(source)
            self.assertEqual(len(rows), (max_exponent + 1) * 12 + 20000)
            self.assertEqual(rows, casts.inputs(source))
            self.assertTrue(all(0 <= raw < 1 << source for raw in rows))
            edges = set(rows[:(max_exponent + 1) * 12])
            for sign in (0, 1):
                for exponent in range(max_exponent + 1):
                    for fraction in (0, 1, (1 << fraction_bits) - 1):
                        self.assertIn(sign << (source - 1) | exponent << fraction_bits | fraction, edges)

    def test_python_float_oracle_matches_independent_fraction_oracle(self):
        for source in (32, 64):
            for bits in (64, 128):
                for signed in (False, True):
                    case = dict(op='float_to_int', bits=bits, source_bits=source, signed=signed)
                    for raw in casts.inputs(source):
                        low, high, _, _ = conversions.expected(case, (raw, 0, 0, 0, 0, 0))
                        self.assertEqual(casts.oracle(raw, source, bits, signed), low | high << 64)


if __name__ == '__main__':
    unittest.main()
