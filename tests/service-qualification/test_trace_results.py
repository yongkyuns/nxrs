import unittest

from trace_results import parse_trace, verify_workers, verify_entry


def base_transcript():
    return (
        "SQ_MEMORY before=0 full=4096 delta=4096 metadata=300 payload=816 stacks=12288\n"
        "SQ_RESULT services=3 queues=9 events=2 received=2 errors=0 mean_cycles=240 "
        "max_cycles=480 misses_1ms=0 source=messages\n"
        "SQ_DONE status=0 heap_after=0\n"
    )


def trace_row(service_id, *, events=2, transit=0, worker=0, led=0, end=0, errors=0):
    return (
        f"SQ_TRACE id={service_id} events={events} transit_cycles={transit} "
        f"worker_cycles={worker} led_cycles={led} end_cycles={end} errors={errors}\n"
    )


def valid_transcript(rows=None):
    if rows is None:
        # Stage 0 closes independently. The complete path through LED (stage
        # 1) telescopes to its end time; stage 2 lies beyond the measured path.
        rows = (
            trace_row(0, transit=100, worker=50, end=150),
            trace_row(1, transit=200, worker=300, led=80, end=650),
            trace_row(2, transit=10, worker=20, end=30),
        )
    return base_transcript() + "".join(rows)


class TraceResultsTests(unittest.TestCase):
    def test_entry_selection_requires_one_exact_marker(self):
        marker = "SQ_ENTRY_SELECTED language=c\n"
        self.assertTrue(verify_entry(marker, language="c"))
        for bad in ("", marker + marker, "SQ_ENTRY_SELECTED language=rust", marker + "SQ_ENTRY_SELECTED x=c"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                verify_entry(bad, language="c")

    def test_worker_selection_requires_exact_complete_markers(self):
        text = "\n".join(f"SQ_WORKER_SELECTED id={i} language=c" for i in range(3))
        self.assertTrue(verify_workers(text, services=3, language="c"))
        for wrong in (text + "\nSQ_WORKER_SELECTED id=0 language=c", text.replace("id=2", "id=3"),
                      text.replace("id=0 language=c", "id=0 language=rust"), text.replace("id=0", "id=-1")):
            with self.subTest(text=wrong), self.assertRaises(ValueError):
                verify_workers(wrong, services=3, language="c")

    def test_valid_trace_returns_base_result_rows_and_per_message_path(self):
        parsed = parse_trace(valid_transcript(), services=3, events=2)

        self.assertEqual(parsed["result"]["source"], "messages")
        self.assertEqual([row["id"] for row in parsed["trace_rows"]], [0, 1, 2])
        self.assertEqual(parsed["instrumentation"], True)
        self.assertEqual(parsed["path_us"], {
            "transit": 0.625,
            "worker_excluding_led": 0.5625,
            "led": 0.16666666666666666,
            "end": 1.3541666666666667,
        })

    def test_rows_are_sorted_even_when_input_order_differs(self):
        text = valid_transcript((
            trace_row(2, transit=10, worker=20, end=30),
            trace_row(1, transit=200, worker=300, led=80, end=650),
            trace_row(0, transit=100, worker=50, end=150),
        ))
        parsed = parse_trace(text, services=3, events=2)
        self.assertEqual([row["id"] for row in parsed["trace_rows"]], [0, 1, 2])

    def test_unsigned_totals_can_exceed_a_wrapped_counter_width(self):
        # Per-event deltas can wrap in the target's 32-bit counter while their
        # accumulated sums are represented as uint64 in the emitted protocol.
        text = valid_transcript((
            trace_row(0, transit=4_294_967_300, worker=50, end=4_294_967_350),
            trace_row(1, transit=200, worker=300, led=80, end=4_294_967_850),
            trace_row(2, transit=10, worker=20, end=30),
        ))
        parsed = parse_trace(text, services=3, events=2)
        self.assertEqual(parsed["trace_rows"][0]["transit_cycles"], 4_294_967_300)

    def test_ansi_around_trace_marker_is_ignored(self):
        text = valid_transcript().replace(
            "SQ_TRACE id=1", "\x1b[1mSQ_TRACE id=1\x1b[0m"
        )
        self.assertEqual(parse_trace(text, services=3, events=2)["trace_rows"][1]["id"], 1)

    def test_missing_and_duplicate_ids_are_rejected(self):
        good = (
            trace_row(0, transit=100, worker=50, end=150),
            trace_row(1, transit=200, worker=300, led=80, end=650),
            trace_row(2, transit=10, worker=20, end=30),
        )
        cases = (
            good[:2],
            (good[0], good[1], good[2], good[2]),
            (good[0], good[1], good[2].replace("id=2", "id=3")),
        )
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_trace(valid_transcript(rows), services=3, events=2)

    def test_malformed_duplicate_fields_and_negative_values_are_rejected(self):
        malformed = (
            "SQ_TRACE id=0 events=2 transit_cycles=100 worker_cycles=50 led_cycles=0 end_cycles=150\n",
            "SQ_TRACE id=0 events=2 transit_cycles=100 worker_cycles=50 led_cycles=0 end_cycles=150 errors=0 extra=1\n",
            "SQ_TRACE id=0 events=2 events=2 transit_cycles=100 worker_cycles=50 led_cycles=0 end_cycles=150 errors=0\n",
            "SQ_TRACE id=-1 events=2 transit_cycles=100 worker_cycles=50 led_cycles=0 end_cycles=150 errors=0\n",
            "SQ_TRACE id=0 events=2 transit_cycles=-1 worker_cycles=51 led_cycles=0 end_cycles=150 errors=0\n",
        )
        for row in malformed:
            with self.subTest(row=row), self.assertRaises(ValueError):
                parse_trace(valid_transcript((row,)), services=3, events=2)

    def test_wrong_event_count_and_reported_errors_are_rejected(self):
        for rows in (
            (trace_row(0, events=1, transit=100, worker=50, end=150),
             trace_row(1, transit=200, worker=300, led=80, end=650),
             trace_row(2, transit=10, worker=20, end=30)),
            (trace_row(0, transit=100, worker=50, end=150, errors=1),
             trace_row(1, transit=200, worker=300, led=80, end=650),
             trace_row(2, transit=10, worker=20, end=30)),
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_trace(valid_transcript(rows), services=3, events=2)

    def test_led_cycles_are_led_service_only_and_cannot_exceed_worker(self):
        invalid = (
            (trace_row(0, transit=100, worker=50, led=1, end=151),
             trace_row(1, transit=199, worker=300, led=80, end=650),
             trace_row(2, transit=10, worker=20, end=30)),
            (trace_row(0, transit=100, worker=50, end=150),
             trace_row(1, transit=200, worker=70, led=80, end=650),
             trace_row(2, transit=10, worker=20, end=30)),
        )
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_trace(valid_transcript(rows), services=3, events=2)

    def test_path_and_stage_closure_failures_are_rejected(self):
        invalid = (
            # The aggregate through stage 1 no longer equals the LED end.
            (trace_row(0, transit=100, worker=50, end=150),
             trace_row(1, transit=201, worker=300, led=80, end=650),
             trace_row(2, transit=10, worker=20, end=30)),
            # Stage 0 must close exactly at its own transit + worker sum.
            (trace_row(0, transit=100, worker=50, end=151),
             trace_row(1, transit=199, worker=300, led=80, end=650),
             trace_row(2, transit=10, worker=20, end=30)),
            # Each stage's end must cover at least its worker cycles.
            (trace_row(0, transit=100, worker=151, end=251),
             trace_row(1, transit=200, worker=300, led=80, end=650),
             trace_row(2, transit=10, worker=31, end=30)),
        )
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_trace(valid_transcript(rows), services=3, events=2)

    def test_base_transcript_must_be_valid_and_use_message_source(self):
        text = valid_transcript().replace("source=messages", "source=gpio")
        with self.assertRaises(ValueError):
            parse_trace(text, services=3, events=2)


if __name__ == "__main__":
    unittest.main()
