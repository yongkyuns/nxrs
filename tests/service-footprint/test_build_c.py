import unittest
from build_c import validate_c_symbols


class CControlTests(unittest.TestCase):
    def test_requires_exact_defined_entry_and_no_rust_runtime(self):
        valid = '42010000 T cq_c_scale_main\n42010020 T nx_start\n'
        validate_c_symbols(valid, 'cq_c_scale')
        for wrong in ('42010000 U cq_c_scale_main\n',
                      '42010000 T cq_c_scale_main_extra\n',
                      valid + '42010040 T __rust_alloc\n',
                      valid + '42010040 T cq_scale::worker\n',
                      valid + '42010040 T std::io::cleanup\n',
                      valid + '42010040 T _RNvRustSymbol\n'):
            with self.subTest(symbols=wrong), self.assertRaises(ValueError):
                validate_c_symbols(wrong, 'cq_c_scale')


if __name__ == '__main__':
    unittest.main()
