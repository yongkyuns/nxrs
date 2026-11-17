"""Long-call review must not infer a target from stale or conflicting registers."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.xtensa_calls import resolve


class LiteralCalls(unittest.TestCase):
    def test_elf_value_wins_over_comment_and_spill_survives_windowed_call(self):
        assembly = """40370100 <worker>:
40370100: 123481 l32r a8, 40370000 <pool> (42009999 <wrong_comment>)
40370103: 2189 s32i.n a8, a1, 8
40370105: 001005 call8 40370200 <other>
40370108: 2188 l32i.n a8, a1, 8
4037010a: 0008e0 callx8 a8
4037010d: f01d retw.n
"""
        rewritten, calls = resolve(assembly, lambda address: 0x42001234)
        self.assertEqual([c['target'] for c in calls], ['0x42001234'])
        self.assertIn('call8 42001234', rewritten)

    def test_conflicting_branch_or_unknown_path_stays_unresolved(self):
        for alternative in ('l32r a8, 40370004 <pool>', 'l32i a8, a2, 0'):
            assembly = f"""40370100 <worker>:
40370100: 123481 l32r a8, 40370000 <pool>
40370103: 111816 beqz a2, 4037010c <worker+0xc>
40370106: 123481 {alternative}
40370109: 0100c6 j 4037010c <worker+0xc>
4037010c: 0008e0 callx8 a8
4037010f: f01d retw.n
"""
            _, calls = resolve(assembly, lambda address: address + 0x1000)
            self.assertEqual(calls, [])

    def test_call_clobber_and_mutable_callback_cannot_be_literal_targets(self):
        for between in ('001005 call8 40370200 <other>', '002882 l32i a8, a8, 0'):
            assembly = f"""40370100 <worker>:
40370100: 123481 l32r a8, 40370000 <pool>
40370103: {between}
40370106: 0008e0 callx8 a8
40370109: f01d retw.n
"""
            _, calls = resolve(assembly, lambda address: 0x40370300)
            self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
