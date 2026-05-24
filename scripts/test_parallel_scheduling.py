"""TDD — Orchestrator-driven parallel scheduling.

Tests _schedule_next_batch decisions (no LLM calls).
The parallel dispatcher itself is integration-tested by running a real mission
with `execution: "readonly-parallel"` subtasks; that exercises threading +
locks + parallel_lite end-to-end. This file just locks down the scheduling
contract.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.runner import (  # noqa: E402
    _schedule_next_batch,
    _schedule_iter,
    PARALLEL_MAX_WORKERS,
)


def _m(*subtasks) -> dict:
    return {"subtasks": list(subtasks)}


def _s(sid, status="todo", deps=None, execution=None):
    out = {"id": sid, "status": status}
    if deps:
        out["depends_on"] = deps
    if execution:
        out["execution"] = execution
    return out


class TestScheduleBatch(unittest.TestCase):

    def test_single_serial_returns_one(self):
        m = _m(_s("A"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["A"])

    def test_two_serial_no_deps_returns_first_only(self):
        """Two ready serial subtasks → only the first runs (no parallelism)."""
        m = _m(_s("A"), _s("B"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["A"])

    def test_two_parallel_no_deps_batched(self):
        m = _m(_s("A", execution="readonly-parallel"),
               _s("B", execution="readonly-parallel"))
        b = _schedule_next_batch(m)
        self.assertEqual(sorted(s["id"] for s in b), ["A", "B"])

    def test_serial_before_parallel_blocks_batching(self):
        """First-ready is serial → batch returns only the serial one,
        even if later subtasks are parallel."""
        m = _m(_s("A"),
               _s("B", execution="readonly-parallel"),
               _s("C", execution="readonly-parallel"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["A"])

    def test_parallel_first_picks_only_parallel_peers(self):
        """First is parallel → grab all *parallel* peers in ready set,
        skip any serial ones (they wait their turn)."""
        m = _m(_s("A", execution="readonly-parallel"),
               _s("B"),  # serial — should NOT join the batch
               _s("C", execution="readonly-parallel"))
        b = _schedule_next_batch(m)
        ids = sorted(s["id"] for s in b)
        self.assertEqual(ids, ["A", "C"])
        self.assertNotIn("B", ids)

    def test_in_progress_excluded(self):
        m = _m(_s("A", status="in-progress"), _s("B"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["B"])

    def test_done_excluded(self):
        m = _m(_s("A", status="done"), _s("B"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["B"])

    def test_deps_must_be_done(self):
        m = _m(_s("A", status="todo"),
               _s("B", deps=["A"], status="todo"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["A"])

    def test_deps_done_unblocks(self):
        m = _m(_s("A", status="done"),
               _s("B", deps=["A"], status="todo"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["B"])

    def test_deprecated_treated_as_done(self):
        m = _m(_s("A", status="deprecated-by-split"),
               _s("B", deps=["A"], status="todo"))
        b = _schedule_next_batch(m)
        self.assertEqual([s["id"] for s in b], ["B"])

    def test_parallel_capped_at_max_workers(self):
        many = [_s(f"P{i}", execution="readonly-parallel") for i in range(PARALLEL_MAX_WORKERS + 3)]
        m = _m(*many)
        b = _schedule_next_batch(m)
        self.assertEqual(len(b), PARALLEL_MAX_WORKERS)

    def test_empty_when_all_done(self):
        m = _m(_s("A", status="done"), _s("B", status="done"))
        self.assertEqual(_schedule_next_batch(m), [])

    def test_complex_mixed_scenario(self):
        """Realistic: 6 subtasks with mixed exec, partial progress.
            A: done       (already finished)
            B: in-progress  (excluded)
            C: parallel, deps=[A]    ready
            D: parallel, deps=[A]    ready
            E: serial,   deps=[A]    ready  ← later in order
            F: parallel, deps=[A,B]  blocked by B
        First-ready = C (parallel) → batch [C, D].  E waits, F blocked."""
        m = _m(
            _s("A", status="done"),
            _s("B", status="in-progress"),
            _s("C", execution="readonly-parallel", deps=["A"]),
            _s("D", execution="readonly-parallel", deps=["A"]),
            _s("E", deps=["A"]),
            _s("F", execution="readonly-parallel", deps=["A", "B"]),
        )
        b = _schedule_next_batch(m)
        ids = sorted(s["id"] for s in b)
        self.assertEqual(ids, ["C", "D"])


class TestScheduleIterStillWorks(unittest.TestCase):
    """Sanity — _schedule_iter is kept for compat, must still yield serially."""

    def test_yields_in_dep_order(self):
        m = _m(_s("A"), _s("B", deps=["A"]))
        gen = _schedule_iter(m)
        first = next(gen)
        self.assertEqual(first["id"], "A")
        # Mark A done to unblock B
        first["status"] = "done"
        second = next(gen)
        self.assertEqual(second["id"], "B")


if __name__ == "__main__":
    unittest.main(verbosity=2)
