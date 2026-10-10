import unittest

from results import parse


def transcript(memory=None, result=None, done=None):
    memory = memory or "before=0 full=4096 delta=4096 metadata=300 payload=816 stacks=12288"
    result = result or (
        "services=3 queues=9 events=10 received=10 errors=0 mean_cycles=240 "
        "max_cycles=480 misses_1ms=1 source=messages"
    )
    done = done or "status=0 heap_after=0"
    return (
        f"SQ_MEMORY {memory}\nSQ_RESULT {result}\nSQ_DONE {done}\n"
    )


class ResultsTests(unittest.TestCase):
    def test_valid_host_measurement_preserves_values_and_converts_cycles(self):
        parsed = parse(
            "nsh> qualification\n" + transcript() + "nsh> ",
            services=3,
            events=10,
            source="messages",
        )
        self.assertEqual(parsed["memory"], {
            "before": 0, "full": 4096, "delta": 4096,
            "metadata": 300, "payload": 816, "stacks": 12288,
        })
        self.assertEqual(parsed["result"]["received"], 10)
        self.assertEqual(parsed["done"], {"status": 0, "heap_after": 0})
        self.assertEqual(parsed["mean_us"], 1)
        self.assertEqual(parsed["max_us"], 2)
        self.assertEqual(parsed["deadline_us"], 1000)

    def test_ansi_and_unrelated_shell_output_are_ignored(self):
        text = (
            "\x1b[32mNSH: service ready\x1b[0m\n"
            + transcript().replace("SQ_RESULT", "\x1b[1mSQ_RESULT\x1b[0m")
            + "warning: unrelated diagnostic\n"
        )
        self.assertEqual(parse(text, services=3, events=10, source="messages")["done"]["status"], 0)

    def test_gpio_source_is_accepted_when_requested(self):
        result = (
            "services=3 queues=9 events=10 received=10 errors=0 mean_cycles=1 "
            "max_cycles=2 misses_1ms=0 source=gpio"
        )
        self.assertEqual(
            parse(transcript(result=result), services=3, events=10, source="gpio")["result"]["source"],
            "gpio",
        )

    def test_duplicate_missing_and_malformed_protocol_lines_are_rejected(self):
        samples = (
            transcript() + "SQ_DONE status=0 heap_after=0\n",
            "SQ_MEMORY before=0 full=4096 delta=4096 metadata=300 payload=816 stacks=12288\n"
            "SQ_RESULT services=3 queues=9 events=10 received=10 errors=0 mean_cycles=240 "
            "max_cycles=480 misses_1ms=1 source=messages\n",
            transcript(result="services=3 queues=9 events=10 received=10 errors=0 "
                       "mean_cycles=240 max_cycles=480 source=messages"),
            transcript(result="services=3 queues=9 events=10 received=10 received=10 errors=0 "
                       "mean_cycles=240 max_cycles=480 misses_1ms=1 source=messages"),
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(ValueError):
                    parse(sample, services=3, events=10, source="messages")

    def test_unrequested_counts_and_source_are_rejected(self):
        for kwargs in (
            {"services": 4, "events": 10, "source": "messages"},
            {"services": 3, "events": 11, "source": "messages"},
            {"services": 3, "events": 10, "source": "gpio"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                parse(transcript(), **kwargs)

    def test_invalid_counts_footprints_and_memory_accounting_are_rejected(self):
        bad_rows = (
            {"result": "services=3 queues=8 events=10 received=10 errors=0 mean_cycles=240 max_cycles=480 misses_1ms=1 source=messages"},
            {"memory": "before=0 full=4096 delta=4096 metadata=300 payload=816 stacks=12287"},
            {"memory": "before=0 full=4096 delta=4096 metadata=300 payload=815 stacks=12288"},
            {"memory": "before=10 full=9 delta=0 metadata=300 payload=816 stacks=12288"},
            {"memory": "before=0 full=4096 delta=4095 metadata=300 payload=816 stacks=12288"},
        )
        for change in bad_rows:
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse(transcript(**change), services=3, events=10, source="messages")

    def test_errors_status_receives_and_timing_constraints_are_rejected(self):
        bad_rows = (
            {"result": "services=3 queues=9 events=10 received=10 errors=1 mean_cycles=240 max_cycles=480 misses_1ms=1 source=messages"},
            {"done": "status=1 heap_after=0"},
            {"result": "services=3 queues=9 events=10 received=9 errors=0 mean_cycles=240 max_cycles=480 misses_1ms=1 source=messages"},
            {"result": "services=3 queues=9 events=10 received=10 errors=0 mean_cycles=481 max_cycles=480 misses_1ms=1 source=messages"},
            {"result": "services=3 queues=9 events=10 received=10 errors=0 mean_cycles=0 max_cycles=0 misses_1ms=1 source=messages"},
            {"result": "services=3 queues=9 events=10 received=10 errors=0 mean_cycles=240 max_cycles=480 misses_1ms=11 source=messages"},
        )
        for change in bad_rows:
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse(transcript(**change), services=3, events=10, source="messages")

    def test_truncated_output_is_rejected(self):
        with self.assertRaises(ValueError):
            parse("SQ_MEMORY before=0 full=4096 delta=4096 metadata=300 payload=816 stacks=12288\n",
                  services=3, events=10, source="messages")


if __name__ == "__main__":
    unittest.main()
