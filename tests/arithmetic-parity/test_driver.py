"""Compile the real C comparator with deliberately correct/incorrect outputs."""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent

FIXTURE = r'''
#define AQ_HOST 1
#include "driver.c"
const struct aq_case aq_cases[1]={{0}};
const uint32_t aq_case_count=0;
int main(void) {
  uint64_t distance;
  struct aq_result actual={0,0,0,0}, expected={0,0,0,0};
  if (!result_matches(&actual,&expected,0,0,1,&distance)) return 1;
  actual.reserved=1;
  if (result_matches(&actual,&expected,0,0,1,&distance)) return 2;
  actual.reserved=0; actual.lo=UINT64_C(0x80000000);
  if (result_matches(&actual,&expected,32,2,1,&distance)) return 3;
  if (!result_matches(&actual,&expected,32,0,0,&distance)) return 4;
  actual.lo=UINT64_C(0x7fc12345); expected.lo=UINT64_C(0x7fc00000);
  if (!result_matches(&actual,&expected,32,0,1,&distance)) return 5;
  actual.lo=UINT64_C(0x7f800000);
  if (result_matches(&actual,&expected,32,2,1,&distance)) return 6;
  expected.lo=UINT64_C(0x3f800000); actual.lo=expected.lo+1;
  if (!result_matches(&actual,&expected,32,1,1,&distance)||distance!=1) return 7;
  if (result_matches(&actual,&expected,32,0,1,&distance)) return 8;
  actual.lo=UINT64_C(0x8000000000000000);expected.lo=0;
  if (result_matches(&actual,&expected,64,2,1,&distance)) return 9;
  if (!result_matches(&actual,&expected,64,0,0,&distance)) return 10;
  actual.lo=UINT64_C(0xfff8000000000001);expected.lo=UINT64_C(0x7ff8000000000000);
  if (!result_matches(&actual,&expected,64,0,1,&distance)) return 11;
  actual.lo=UINT64_C(0xfff0000000000000);expected.lo=UINT64_C(0x7ff0000000000000);
  if (result_matches(&actual,&expected,64,0,1,&distance)) return 12;
  actual.lo=expected.lo=UINT64_C(0x3ff0000000000000);actual.flag=1;
  if (result_matches(&actual,&expected,64,0,1,&distance)) return 13;
  return 0;
}
'''


class DriverTests(unittest.TestCase):
    def test_real_comparator_rejects_corruption_and_obeys_float_policy(self):
        with tempfile.TemporaryDirectory(prefix="aq-driver-") as folder:
            source = Path(folder)/"fixture.c"
            source.write_text(FIXTURE)
            binary = Path(folder)/"fixture"
            subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(HERE), str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
