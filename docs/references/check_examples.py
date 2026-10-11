#!/usr/bin/env python3
"""Exercise the guide's illustrative arithmetic and failure cases.

These deterministic models are not measurements, scheduler simulations or driver
qualification. Run with `python3 docs/references/check_examples.py`.
"""
from fractions import Fraction
from math import ceil
import unittest
from pathlib import Path


def blackout_arrivals(burst: int, rate: Fraction, blackout: Fraction) -> int:
    if burst < 0 or rate < 0 or blackout < 0:
        raise ValueError("Arrival-bound parameters must be nonnegative")
    return burst + ceil(rate * blackout)


def has_wait_cycle(graph: dict[str, tuple[str, ...]]) -> bool:
    """An edge A->B means A cannot finish until B makes progress."""
    visiting: set[str] = set()
    finished: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in finished:
            return False
        visiting.add(node)
        if any(visit(other) for other in graph.get(node, ())):
            return True
        visiting.remove(node)
        finished.add(node)
        return False

    return any(visit(node) for node in graph)


class GuideExamples(unittest.TestCase):
    def test_blackout_capacity_and_payload_only(self):
        records = blackout_arrivals(4, Fraction(100), Fraction(50, 1000))
        self.assertEqual(records, 9)
        self.assertEqual(records * 64, 576)

    def test_fluid_recovery_is_not_a_deadline_bound(self):
        self.assertEqual(Fraction(9, 200 - 100), Fraction(90, 1000))
        self.assertGreater(Fraction(50, 1000), Fraction(20, 1000))

    def test_discrete_arrival_rounding(self):
        self.assertEqual(blackout_arrivals(4, Fraction(100), Fraction(51, 1000)), 10)
        with self.assertRaises(ValueError):
            blackout_arrivals(-1, Fraction(100), Fraction(1))

    def test_documented_numbers_match_the_model(self):
        guide = (Path(__file__).parent / 'developer-guide.md').read_text()
        for literal in ('B = 4 extra burst records', 'r = 100 records/second',
                        'J = 0.050 seconds', '= 9', '**576 bytes**',
                        '**20 ms output deadline**'):
            self.assertIn(literal, guide)

    def test_shutdown_cycle_and_explicitly_broken_dependency(self):
        blocked = {
            "join": ("producer-exit",),
            "producer-exit": ("queue-space",),
            "queue-space": ("service-drain",),
            "service-drain": ("join",),
        }
        self.assertTrue(has_wait_cycle(blocked))
        # Model only: a real cancellable admission path must be implemented/tested.
        stopped = {**blocked, "producer-exit": ()}
        self.assertFalse(has_wait_cycle(stopped))

    def test_current_state_notifications_differ_from_copies(self):
        # Two references to one channel can both see B; copies retain A and B.
        channel = {"value": "A"}
        references = [channel]
        copies = [channel["value"]]
        channel["value"] = "B"
        references.append(channel)
        copies.append(channel["value"])
        self.assertEqual([ref["value"] for ref in references], ["B", "B"])
        self.assertEqual(copies, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
