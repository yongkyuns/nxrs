"""Fail-closed NuttX admission execution oracle controls; not hardware evidence."""
import unittest
import qualify_admission_target as target


class ExecutionOracleTests(unittest.TestCase):
    def setUp(self):
        self.labels = ["positive", "signed-wrong-target"]
        self.prefixes = 17
        self.lines = ["IMAGE_ADMISSION_CASE " + label + " passed" for label in self.labels]
        self.summary = "IMAGE_ADMISSION_QUALIFIED cases=2 proper_prefixes=17 corruptions=10000"
        self.text = "\n".join(self.lines + [self.summary]) + "\n"
        self.refusal = "IMAGE_ADMISSION_EXPECTED_REFUSAL floor\n"

    def verify(self, text, status="IMAGE_ADMISSION_STATUS_0\n", mode="pass"):
        target.verify_execution(text, status, mode, self.labels, self.prefixes)

    def test_exact_success_and_actual_nonzero_refusal_are_distinct(self):
        self.verify(self.text)
        self.verify(self.refusal, "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal")
        self.verify(self.text.replace("\n", "\r\n"))

    def test_missing_duplicate_reordered_extra_and_wrong_results_are_refused(self):
        for text in (self.text.replace(self.lines[0] + "\n", ""), self.text + self.lines[0] + "\n",
                     "\n".join(list(reversed(self.lines)) + [self.summary]),
                     self.text + "IMAGE_ADMISSION_CASE unexpected passed\n",
                     self.text.replace("corruptions=10000", "corruptions=1"),
                     self.text.replace("proper_prefixes=17", "proper_prefixes=16"),
                     self.text + self.summary + "\n", self.text + "PANIC\n",
                     self.text + "thread panicked at\n", self.text + self.refusal):
            with self.assertRaises(ValueError):
                self.verify(text)

    def test_status_and_negative_oracle_cannot_turn_a_failure_into_success(self):
        for text, status, mode in ((self.text, "IMAGE_ADMISSION_STATUS_1\n", "pass"),
                                  (self.text, "IMAGE_ADMISSION_STATUS_0\n" * 2, "pass"),
                                  (self.refusal, "IMAGE_ADMISSION_STATUS_0\n", "floor-refusal"),
                                  (self.refusal * 2, "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal"),
                                  ("", "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal"),
                                  (self.refusal + self.summary, "IMAGE_ADMISSION_STATUS_1\n", "floor-refusal"),
                                  (self.refusal, "IMAGE_ADMISSION_STATUS_1\n", "unknown")):
            with self.assertRaises(ValueError):
                self.verify(text, status, mode)


if __name__ == "__main__":
    unittest.main()
