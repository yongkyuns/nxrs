"""Regression checks for Embassy completion polling and idle entry."""
from pathlib import Path
import re
import unittest

EMBASSY_SOURCE = Path(__file__).with_name("embassy.rs").read_text()


def while_body(source, condition):
    """Return a while-loop body using brace depth, without parsing Rust code."""
    start = source.index(f"while {condition}")
    opening = source.index("{", start)
    depth = 1
    for index in range(opening + 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"unterminated while loop: {condition}")


def poll_until_done(is_done, poll, sleep_if_idle):
    """Mirror the executor's terminal-poll ordering for the behavioral case."""
    while not is_done():
        poll()
        if not is_done():
            sleep_if_idle()


class ExecutorCompletionTests(unittest.TestCase):
    def test_completion_set_by_last_clock_poll_does_not_idle(self):
        state = {"done": False, "polls": 0, "sleeps": 0}

        def poll():
            state["polls"] += 1
            state["done"] = True

        def sleep_if_idle():
            state["sleeps"] += 1
            self.fail("entered WAITI after the final clock poll, with no alarm armed")

        poll_until_done(lambda: state["done"], poll, sleep_if_idle)
        self.assertEqual(state, {"done": True, "polls": 1, "sleeps": 0})

    def test_each_executor_terminal_loop_checks_completion_after_poll(self):
        cases = (
            ("DONE.load(Ordering::Acquire) != 20", "DONE.load(Ordering::Acquire) != 20"),
            ("!CLOCK_DONE.load(Ordering::Acquire)", "!CLOCK_DONE.load(Ordering::Acquire)"),
        )
        for condition, guard in cases:
            with self.subTest(condition=condition):
                body = while_body(EMBASSY_SOURCE, condition)
                ordered_guard = re.compile(
                    r"poll\(executor\);\s*if\s+" + re.escape(guard) +
                    r"\s*\{\s*sleep_if_idle\(\);\s*\}", re.S)
                self.assertRegex(body, ordered_guard)
                self.assertEqual(body.count("sleep_if_idle();"), 1)

    def test_waiti_keeps_masked_check_and_compiler_fences(self):
        body = while_body(EMBASSY_SOURCE, "!CLOCK_DONE.load(Ordering::Acquire)")
        self.assertIn("if !CLOCK_DONE.load(Ordering::Acquire) {\n                sleep_if_idle();", body)
        idle_start = EMBASSY_SOURCE.index("fn sleep_if_idle()")
        idle_end = EMBASSY_SOURCE.index("fn run(", idle_start)
        idle = EMBASSY_SOURCE[idle_start:idle_end]
        self.assertIn("compiler_fence(Ordering::SeqCst);", idle)
        self.assertIn('asm!("waiti 0"', idle)
        self.assertEqual(idle.count("compiler_fence(Ordering::SeqCst);"), 2)


if __name__ == "__main__":
    unittest.main()
