"""TDD for the MissionLedger — cumulative cross-subtask memory."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.runner import (  # noqa: E402
    _parse_structured_handoff,
    MissionLedger,
    LEDGER_INVARIANT_HARD_CAP,
)


# =============================================================================
# Parser tests — extract Files touched / Invariants / Decisions / Narrative.
# =============================================================================

class TestParser(unittest.TestCase):

    def test_full_structured_handoff(self):
        text = """## Implementation
did stuff

## Handoff
### Files touched
- frontend/game.js
- backend/app.py

### Invariants
- LAYOUT.providers must contain 4 keys: a, b, c, d
- PROVIDER_ABBR is a const map {key: 2-char-string}

### Decisions
- Used Phaser graphics over sprite for HUD perf
- Bumped depth=1100 to render above desk

### Narrative
Added HUD progress bar and ticker. Wired into pollAll.
"""
        out = _parse_structured_handoff(text)
        self.assertEqual(out["files_touched"],
                         ["frontend/game.js", "backend/app.py"])
        self.assertEqual(len(out["invariants"]), 2)
        self.assertIn("4 keys", out["invariants"][0])
        self.assertEqual(len(out["decisions"]), 2)
        self.assertIn("HUD progress bar", out["narrative"])

    def test_no_handoff_section_falls_back_to_tail(self):
        text = "line1\nline2\nline3\nline4\nline5"
        out = _parse_structured_handoff(text)
        self.assertEqual(out["files_touched"], [])
        self.assertEqual(out["invariants"], [])
        self.assertIn("line5", out["narrative"])

    def test_partial_handoff_missing_sections(self):
        """Worker only fills Narrative — others default empty."""
        text = "## Handoff\n### Narrative\nQuick fix to colors."
        out = _parse_structured_handoff(text)
        self.assertEqual(out["files_touched"], [])
        self.assertEqual(out["invariants"], [])
        self.assertIn("Quick fix to colors", out["narrative"])

    def test_bullet_marker_variants(self):
        text = """## Handoff
### Files touched
- a.js
* b.js
• c.js
no-bullet.js
"""
        out = _parse_structured_handoff(text)
        self.assertEqual(set(out["files_touched"]),
                         {"a.js", "b.js", "c.js", "no-bullet.js"})

    def test_stops_at_next_h2(self):
        text = """## Handoff
### Invariants
- X must Y

## Some other section
should-not-leak-in
"""
        out = _parse_structured_handoff(text)
        self.assertEqual(out["invariants"], ["X must Y"])

    def test_narrative_capped_at_800_chars(self):
        big = "word " * 500   # ~2500 chars
        text = f"## Handoff\n### Narrative\n{big}"
        out = _parse_structured_handoff(text)
        self.assertLess(len(out["narrative"]), 900)
        self.assertIn("truncated", out["narrative"])


# =============================================================================
# Ledger accumulation + persistence tests.
# =============================================================================

class TestLedger(unittest.TestCase):

    def _make(self) -> tuple[MissionLedger, Path]:
        td = tempfile.mkdtemp()
        return MissionLedger.load_or_new(Path(td)), Path(td)

    def test_empty_ledger_returns_empty_context(self):
        led, _ = self._make()
        self.assertEqual(led.as_worker_context(), "")

    def test_record_persists_to_ledger_json(self):
        led, td = self._make()
        led.record("T-01", "## Handoff\n### Narrative\nDid the thing.")
        self.assertTrue((td / "ledger.json").exists())
        data = json.loads((td / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "T-01")
        self.assertIn("Did the thing", data[0]["narrative"])

    def test_load_or_new_resumes_from_disk(self):
        led1, td = self._make()
        led1.record("T-01", "## Handoff\n### Files touched\n- a.js\n### Narrative\nfirst")
        led1.record("T-02", "## Handoff\n### Files touched\n- b.js\n### Narrative\nsecond")
        # Simulate process restart
        led2 = MissionLedger.load_or_new(td)
        self.assertEqual(len(led2.entries), 2)
        self.assertEqual(led2.entries[0]["id"], "T-01")

    def test_invariants_aggregate_dedupe(self):
        led, _ = self._make()
        led.record("T-01", "## Handoff\n### Invariants\n- LAYOUT has 4 keys")
        led.record("T-02", "## Handoff\n### Invariants\n- LAYOUT has 4 keys\n- depth=1100")
        invs = led.aggregate_invariants()
        self.assertEqual(len(invs), 2)

    def test_invariants_capped(self):
        led, _ = self._make()
        # Pump in more invariants than the cap
        for i in range(LEDGER_INVARIANT_HARD_CAP + 10):
            led.record(f"T-{i}", f"## Handoff\n### Invariants\n- inv #{i}")
        invs = led.aggregate_invariants()
        self.assertLessEqual(len(invs), LEDGER_INVARIANT_HARD_CAP)

    def test_files_index_tracks_last_toucher(self):
        led, _ = self._make()
        led.record("T-01", "## Handoff\n### Files touched\n- shared.js")
        led.record("T-02", "## Handoff\n### Files touched\n- shared.js")
        idx = dict(led.files_index())
        self.assertEqual(idx["shared.js"], "T-02")

    def test_recent_narratives_default_count(self):
        led, _ = self._make()
        for i in range(5):
            led.record(f"T-{i}", f"## Handoff\n### Narrative\nstep {i}")
        recent = led.recent_narratives()
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[-1]["id"], "T-4")

    def test_worker_context_includes_invariants_files_and_narratives(self):
        led, _ = self._make()
        led.record("T-01",
                   "## Handoff\n### Files touched\n- a.js\n"
                   "### Invariants\n- A must B\n"
                   "### Narrative\nstep one")
        led.record("T-02",
                   "## Handoff\n### Files touched\n- b.js\n"
                   "### Invariants\n- C must D\n"
                   "### Narrative\nstep two")
        ctx = led.as_worker_context()
        self.assertIn("Invariants established", ctx)
        self.assertIn("A must B", ctx)
        self.assertIn("C must D", ctx)
        self.assertIn("Files touched so far", ctx)
        self.assertIn("a.js", ctx)
        self.assertIn("b.js", ctx)
        self.assertIn("Recent subtask handoffs", ctx)
        self.assertIn("T-02", ctx)

    def test_corrupt_ledger_json_does_not_crash(self):
        td = tempfile.mkdtemp()
        (Path(td) / "ledger.json").write_text("not valid json", encoding="utf-8")
        led = MissionLedger.load_or_new(Path(td))
        self.assertEqual(led.entries, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
