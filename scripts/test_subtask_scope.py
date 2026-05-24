"""TDD — v0.4.0 subtask scope enforcement + structured AC checks.

Validates two new architecture pieces:
  1. _path_in_scope: glob matching for subtask.scope.writes allow-list
  2. _apply_files_to_write: rejects writes outside scope
  3. _run_structured_ac_checks: mechanical AC pass/fail with file-scoped grep
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.runner import (  # noqa: E402
    _path_in_scope,
    _apply_files_to_write,
    _run_structured_ac_checks,
)


def _silent(*a, **kw):
    pass


class TestPathInScope(unittest.TestCase):

    def test_no_scope_allows_anything(self):
        self.assertTrue(_path_in_scope("anywhere.txt", []))
        self.assertTrue(_path_in_scope("frontend/layout.js", None or []))

    def test_exact_match(self):
        scope = ["frontend/layout.js"]
        self.assertTrue(_path_in_scope("frontend/layout.js", scope))
        self.assertFalse(_path_in_scope("frontend/game.js", scope))

    def test_single_star_glob(self):
        scope = ["frontend/widgets/*.js"]
        self.assertTrue(_path_in_scope("frontend/widgets/sparkline.js", scope))
        self.assertTrue(_path_in_scope("frontend/widgets/idle-chatter.js", scope))
        # Subdir doesn't match single *
        self.assertFalse(_path_in_scope("frontend/widgets/sub/x.js", scope))
        self.assertFalse(_path_in_scope("frontend/layout.js", scope))

    def test_double_star_recursive(self):
        scope = ["frontend/**/*.js"]
        self.assertTrue(_path_in_scope("frontend/widgets/sparkline.js", scope))
        self.assertTrue(_path_in_scope("frontend/widgets/sub/deep/x.js", scope))
        self.assertTrue(_path_in_scope("frontend/game.js", scope))
        self.assertFalse(_path_in_scope("backend/app.py", scope))

    def test_multiple_patterns_or_logic(self):
        scope = ["frontend/layout.js", "frontend/widgets/*.js"]
        self.assertTrue(_path_in_scope("frontend/layout.js", scope))
        self.assertTrue(_path_in_scope("frontend/widgets/x.js", scope))
        self.assertFalse(_path_in_scope("frontend/game.js", scope))

    def test_backslash_normalized(self):
        scope = ["frontend/layout.js"]
        self.assertTrue(_path_in_scope("frontend\\layout.js", scope))


class TestApplyFilesToWriteWithScope(unittest.TestCase):

    def _files_block(self, *entries) -> str:
        body = "\n".join(f"### {p}\n```\n{c}\n```\n" for p, c in entries)
        return f"## FILES_TO_WRITE\n\n{body}"

    def test_within_scope_writes(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            out = self._files_block(
                ("frontend/layout.js", "const LAYOUT = {};"),
            )
            n = _apply_files_to_write(out, d, _silent, "T-01",
                                      scope_writes=["frontend/layout.js"])
            self.assertEqual(n, 1)
            self.assertTrue((d / "frontend" / "layout.js").exists())

    def test_outside_scope_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            out = self._files_block(
                ("frontend/layout.js", "ok"),
                ("frontend/game.js", "BLOCKED"),
            )
            captured = []
            def cap(event, **kw): captured.append((event, kw))
            n = _apply_files_to_write(out, d, cap, "T-01",
                                      scope_writes=["frontend/layout.js"])
            self.assertEqual(n, 1)  # only layout.js wrote
            self.assertTrue((d / "frontend" / "layout.js").exists())
            self.assertFalse((d / "frontend" / "game.js").exists())
            # Verify the block was logged
            events = [e for e, _ in captured]
            self.assertIn("files-out-of-scope", events)

    def test_no_scope_unrestricted(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            out = self._files_block(
                ("frontend/anywhere.js", "x"),
            )
            n = _apply_files_to_write(out, d, _silent, "T-01", scope_writes=None)
            self.assertEqual(n, 1)


class TestStructuredACChecks(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.d = Path(self.td)
        target = self.d / "code.js"
        target.parent.mkdir(exist_ok=True)
        target.write_text(
            "const LAYOUT = { sparkline: { x: 120 } };\n"
            "const OTHER = 42;\n",
            encoding="utf-8",
        )

    def test_must_contain_pass(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js", "must_contain": "LAYOUT.sparkline"}}
             if False else  # not exact substring
             {"id": "AC-1", "check": {"file": "code.js", "must_contain": "sparkline"}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "pass")

    def test_must_contain_fail(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js", "must_contain": "NOTHERE"}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "fail")
        self.assertIn("missing", results[0]["evidence"])

    def test_must_contain_list(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js",
                                      "must_contain": ["LAYOUT", "sparkline", "OTHER"]}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "pass")

    def test_must_not_contain_pass(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js",
                                      "must_not_contain": "...existing config..."}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "pass")

    def test_must_not_contain_fail(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js",
                                      "must_not_contain": "LAYOUT"}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "fail")
        self.assertIn("forbidden", results[0]["evidence"])

    def test_byte_floor_ratio_fail(self):
        # Pretend the file used to be 1000 bytes; now it's ~60 → ratio < 0.95
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js", "byte_floor_ratio": 0.95}}],
            self.d, bytes_before={"code.js": 1000},
        )
        self.assertEqual(results[0]["verdict"], "fail")
        self.assertIn("shrunk", results[0]["evidence"])

    def test_byte_floor_ratio_pass(self):
        # Tiny growth — ratio 1.06
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js", "byte_floor_ratio": 0.95}}],
            self.d, bytes_before={"code.js": 60},
        )
        self.assertEqual(results[0]["verdict"], "pass")

    def test_regex_match(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "code.js",
                                      "regex_match": r"sparkline:\s*\{"}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "pass")

    def test_file_not_found(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {"file": "doesnotexist.js", "must_contain": "x"}}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "fail")
        self.assertIn("not found", results[0]["evidence"])

    def test_no_check_field_unchecked(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "desc": "subjective check, no structured spec"}],
            self.d, bytes_before={},
        )
        self.assertEqual(results[0]["verdict"], "unchecked")

    def test_combined_checks(self):
        results = _run_structured_ac_checks(
            [{"id": "AC-1", "check": {
                "file": "code.js",
                "must_contain": "sparkline",
                "must_not_contain": "...existing config...",
                "byte_floor_ratio": 0.5,
            }}],
            self.d, bytes_before={"code.js": 60},
        )
        self.assertEqual(results[0]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
