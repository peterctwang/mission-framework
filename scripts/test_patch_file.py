"""TDD — Minimax patch_file tool: surgical find/replace, never wipes context.

Validates the v0.3.8 fix for the "Minimax rewrites whole file with stub"
production failure mode. patch_file requires:
  - file exists
  - `find` string appears EXACTLY ONCE
  - returns clear error otherwise
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.providers.minimax_token import MinimaxToken  # noqa: E402


def _dispatch(name: str, args: dict, cwd: Path) -> str:
    return MinimaxToken._dispatch_tool(name, args, cwd)


class TestPatchFileSuccess(unittest.TestCase):

    def test_unique_find_replaces_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            target = d / "layout.js"
            target.write_text(
                "const LAYOUT = {\n"
                "  game: { width: 1280 },\n"
                "  providers: { 'claude-cli': {} },\n"
                "};\n",
                encoding="utf-8",
            )
            result = _dispatch("patch_file", {
                "path": "layout.js",
                "find":    "  providers: { 'claude-cli': {} },\n};",
                "replace": "  providers: { 'claude-cli': {} },\n  newKey: 42,\n};",
            }, d)
            self.assertIn("OK", result)
            content = target.read_text(encoding="utf-8")
            self.assertIn("newKey: 42", content)
            # Critically: existing keys still there
            self.assertIn("game: { width: 1280 }", content)
            self.assertIn("'claude-cli'", content)

    def test_preserves_other_content(self):
        """The hallmark test — patch_file must NOT wipe unrelated lines."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            big = "// header\n" + "\n".join(f"const X{i} = {i};" for i in range(50))
            big += "\n// end of file\n"
            target = d / "big.js"
            target.write_text(big, encoding="utf-8")
            _dispatch("patch_file", {
                "path": "big.js",
                "find":    "const X25 = 25;",
                "replace": "const X25 = 9999;  // patched",
            }, d)
            content = target.read_text(encoding="utf-8")
            self.assertIn("const X25 = 9999;", content)
            # Every other line preserved
            for i in range(50):
                if i == 25:
                    continue
                self.assertIn(f"const X{i} = {i};", content)
            self.assertIn("// header", content)
            self.assertIn("// end of file", content)


class TestPatchFileErrors(unittest.TestCase):

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            r = _dispatch("patch_file",
                          {"path": "nope.js", "find": "x", "replace": "y"},
                          Path(td))
            self.assertTrue(r.startswith("ERROR"))
            self.assertIn("does not exist", r)

    def test_find_not_in_file(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "f.txt").write_text("hello world", encoding="utf-8")
            r = _dispatch("patch_file",
                          {"path": "f.txt", "find": "ZZZ", "replace": "yyy"},
                          d)
            self.assertTrue(r.startswith("ERROR"))
            self.assertIn("not found", r)
            # Should hint that the model needs read_file first
            self.assertIn("read_file", r.lower())

    def test_find_multiple_occurrences(self):
        """If `find` appears 2+ times, refuse — forces specificity."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "f.txt").write_text("abc\nabc\nabc\n", encoding="utf-8")
            r = _dispatch("patch_file",
                          {"path": "f.txt", "find": "abc", "replace": "X"},
                          d)
            self.assertTrue(r.startswith("ERROR"))
            self.assertIn("appears 3 times", r)
            self.assertIn("specific", r.lower())

    def test_empty_find_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "f.txt").write_text("anything", encoding="utf-8")
            r = _dispatch("patch_file",
                          {"path": "f.txt", "find": "", "replace": "anything"},
                          d)
            self.assertTrue(r.startswith("ERROR"))
            self.assertIn("`find` is empty", r)

    def test_unsafe_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            r = _dispatch("patch_file",
                          {"path": "../escape.txt", "find": "x", "replace": "y"},
                          Path(td))
            self.assertTrue(r.startswith("ERROR"))
            self.assertIn("unsafe", r.lower())

    def test_absolute_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            r = _dispatch("patch_file",
                          {"path": "/etc/passwd", "find": "x", "replace": "y"},
                          Path(td))
            self.assertTrue(r.startswith("ERROR"))
            self.assertIn("unsafe", r.lower())


class TestPatchFileSchema(unittest.TestCase):
    """The tool must be advertised to Minimax so the model knows it exists."""

    def test_schema_has_patch_file(self):
        names = [t["function"]["name"] for t in MinimaxToken.TOOLS_SCHEMA]
        self.assertIn("patch_file", names)

    def test_schema_required_params(self):
        for t in MinimaxToken.TOOLS_SCHEMA:
            if t["function"]["name"] == "patch_file":
                req = set(t["function"]["parameters"]["required"])
                self.assertEqual(req, {"path", "find", "replace"})

    def test_write_file_warning_present(self):
        """write_file description must warn against using it for edits —
        otherwise the model keeps reaching for it."""
        for t in MinimaxToken.TOOLS_SCHEMA:
            if t["function"]["name"] == "write_file":
                desc = t["function"]["description"]
                self.assertIn("patch_file", desc)
                self.assertIn("NOT USE", desc.upper().replace("DO NOT USE", "NOT USE"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
