"""TDD parity suite — Gemini CLI must do everything Claude/Codex CLIs can do.

Run all:                python -m scripts.test_gemini_parity
Run only fast unit:     python -m scripts.test_gemini_parity --unit
Skip slow integration:  GEMINI_SKIP_INTEGRATION=1 python -m scripts.test_gemini_parity

Each integration test costs ~5-30s of Gemini quota. The full suite costs
roughly 1-2 minutes wall-time and a few thousand tokens.

Parity surface tested:
  Unit:    _parse (4 envelopes), _build_env (4 vars), directive structure
  Basic:   non-empty text, usage tokens > 0
  Modes:   validator (cwd=None, default approval), worker (cwd=path, yolo)
  Tools:   write_file, read_file, edit (round-trip), shell command
  Multi:   read-then-write (2-step trajectory)
  Edge:    long prompt (>8K argv would truncate), unicode, multiline,
           verdict-style output, quota-signal stderr mapping
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure repo root on path so we can import harness.* when launched as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.providers.gemini_oauth import GeminiOAuth, _build_env, _DIRECTIVE  # noqa: E402
from harness.providers.base import QuotaExhausted, TransientProviderError, Usage  # noqa: E402


SKIP_INTEGRATION = os.environ.get("GEMINI_SKIP_INTEGRATION") == "1"
INTEGRATION_REASON = "GEMINI_SKIP_INTEGRATION=1 set (saves quota)"


# =============================================================================
# 1. UNIT TESTS — no subprocess; verify parser + env + directive structure.
# =============================================================================

class TestParse(unittest.TestCase):
    """_parse must extract text + usage from every envelope shape Gemini emits."""

    def test_new_envelope_stats_models_tokens(self):
        """May 2026 envelope: stats.models.<model>.tokens.{prompt,candidates,cached,total}."""
        stdout = json.dumps({
            "response": "answer 42",
            "stats": {
                "models": {
                    "gemini-2.5-pro": {
                        "tokens": {"prompt": 100, "candidates": 20, "cached": 5, "total": 125}
                    }
                }
            }
        })
        text, usage = GeminiOAuth._parse(stdout, model="gemini-2.5-pro")
        self.assertEqual(text, "answer 42")
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 20)
        self.assertEqual(usage.cached_input_tokens, 5)

    def test_new_envelope_picks_first_model_when_mismatch(self):
        stdout = json.dumps({
            "response": "ok",
            "stats": {"models": {"gemini-1.5-pro": {"tokens": {"prompt": 9, "candidates": 1}}}}
        })
        text, usage = GeminiOAuth._parse(stdout, model="gemini-2.5-pro")
        self.assertEqual(text, "ok")
        self.assertEqual(usage.input_tokens, 9)
        self.assertEqual(usage.output_tokens, 1)

    def test_legacy_envelope_usage_metadata(self):
        """Older envelope without stats — fall back to top-level usage."""
        stdout = json.dumps({
            "response": "old answer",
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10}
        })
        text, usage = GeminiOAuth._parse(stdout, model="gemini-2.5-pro")
        self.assertEqual(text, "old answer")
        self.assertEqual(usage.input_tokens, 50)
        self.assertEqual(usage.output_tokens, 10)

    def test_empty_output(self):
        text, usage = GeminiOAuth._parse("", model="gemini-2.5-pro")
        self.assertEqual(text, "")
        self.assertEqual(usage.input_tokens, 0)

    def test_ndjson_stream_fallback(self):
        """If JSON parse fails, last `response` line of NDJSON wins."""
        stdout = '\n'.join([
            json.dumps({"event": "tool_use", "name": "write_file"}),
            json.dumps({"response": "first"}),
            json.dumps({"event": "tool_result"}),
            json.dumps({"response": "final"}),
        ])
        # Append a leading non-JSON char to force JSON.parse failure
        broken = "X\n" + stdout
        text, _ = GeminiOAuth._parse(broken, model="gemini-2.5-pro")
        self.assertEqual(text, "final")

    def test_garbage_input(self):
        text, usage = GeminiOAuth._parse("not json at all", model="gemini-2.5-pro")
        self.assertEqual(text, "")
        self.assertEqual(usage.input_tokens, 0)


class TestBuildEnv(unittest.TestCase):
    """_build_env hardens the env against TUI / auto-update hangs."""

    def test_sets_all_four_defensive_vars(self):
        """In a stripped env, all 4 vars must default to safe values."""
        with patch.dict(os.environ, {}, clear=True):
            env = _build_env()
            self.assertEqual(env["NO_COLOR"], "1")
            self.assertEqual(env["TERM"], "dumb")
            self.assertEqual(env["GEMINI_CLI_DISABLE_TELEMETRY"], "1")
            self.assertEqual(env["GEMINI_CLI_DISABLE_AUTO_UPDATE"], "1")

    def test_does_not_override_existing(self):
        with patch.dict(os.environ, {"NO_COLOR": "user-value"}, clear=False):
            env = _build_env()
            self.assertEqual(env["NO_COLOR"], "user-value")

    def test_preserves_path(self):
        env = _build_env()
        self.assertIn("PATH", env)


class TestDirective(unittest.TestCase):
    """The directive preamble must keep Gemini in single-shot mode."""

    def test_directive_mentions_single_shot(self):
        self.assertIn("single-shot", _DIRECTIVE.lower())

    def test_directive_forbids_filesystem_exploration(self):
        self.assertIn("filesystem", _DIRECTIVE.lower())

    def test_directive_includes_validator_verdict_anchor(self):
        # Validator role's machine-parseable last line.
        self.assertIn("判決:通過", _DIRECTIVE)
        self.assertIn("判決:打回", _DIRECTIVE)


# =============================================================================
# 2. ERROR HANDLING — mocked subprocess to inject error scenarios.
# =============================================================================

class TestErrorHandling(unittest.TestCase):
    """Error paths must raise the right exception (Quota vs Transient)."""

    def _mock_proc(self, returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_quota_exceeded_stderr_raises_quota(self, _res, mrun):
        mrun.return_value = self._mock_proc(1, stderr="Error: quota exceeded for project")
        g = GeminiOAuth()
        with self.assertRaises(QuotaExhausted) as ctx:
            g.generate("sys", "user")
        self.assertEqual(ctx.exception.provider, "gemini-cli")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_rate_limit_raises_quota(self, _res, mrun):
        mrun.return_value = self._mock_proc(1, stderr="429: rate limit exceeded")
        with self.assertRaises(QuotaExhausted):
            GeminiOAuth().generate("sys", "user")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_resource_exhausted_raises_quota(self, _res, mrun):
        mrun.return_value = self._mock_proc(1, stderr="RESOURCE_EXHAUSTED")
        with self.assertRaises(QuotaExhausted):
            GeminiOAuth().generate("sys", "user")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_generic_failure_raises_transient(self, _res, mrun):
        mrun.return_value = self._mock_proc(1, stderr="Unexpected: file not found")
        with self.assertRaises(TransientProviderError):
            GeminiOAuth().generate("sys", "user")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_timeout_raises_transient(self, _res, mrun):
        mrun.side_effect = subprocess.TimeoutExpired(cmd="gemini", timeout=1800)
        with self.assertRaises(TransientProviderError):
            GeminiOAuth().generate("sys", "user")


class TestCommandConstruction(unittest.TestCase):
    """Verify the actual argv passed to subprocess.run matches spec."""

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_validator_mode_uses_default_approval(self, _res, mrun):
        mrun.return_value = MagicMock(returncode=0, stdout='{"response":"ok"}', stderr="")
        GeminiOAuth().generate("sys", "user")  # no cwd → validator
        argv = mrun.call_args[0][0]
        self.assertIn("--approval-mode", argv)
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "default")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_worker_mode_uses_yolo_approval(self, _res, mrun):
        mrun.return_value = MagicMock(returncode=0, stdout='{"response":"ok"}', stderr="")
        GeminiOAuth().generate("sys", "user", cwd="/tmp")
        argv = mrun.call_args[0][0]
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "yolo")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_required_flags_present(self, _res, mrun):
        mrun.return_value = MagicMock(returncode=0, stdout='{"response":"ok"}', stderr="")
        GeminiOAuth().generate("sys", "user")
        argv = mrun.call_args[0][0]
        for required in ("-m", "gemini-2.5-pro", "-o", "json", "--skip-trust", "-p"):
            self.assertIn(required, argv, f"missing required flag/value: {required}")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_does_not_use_deprecated_yolo_flag(self, _res, mrun):
        """`--yolo` (standalone) is deprecated; only --approval-mode yolo is valid."""
        mrun.return_value = MagicMock(returncode=0, stdout='{"response":"ok"}', stderr="")
        GeminiOAuth().generate("sys", "user", cwd="/tmp")
        argv = mrun.call_args[0][0]
        self.assertNotIn("--yolo", argv)

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_never_uses_broken_plan_mode(self, _res, mrun):
        """`--approval-mode plan` is broken on Windows headless — must never appear."""
        mrun.return_value = MagicMock(returncode=0, stdout='{"response":"ok"}', stderr="")
        for cwd in (None, "/tmp"):
            mrun.reset_mock()
            GeminiOAuth().generate("sys", "user", cwd=cwd)
            argv = mrun.call_args[0][0]
            idx = argv.index("--approval-mode")
            self.assertNotEqual(argv[idx + 1], "plan",
                                f"plan mode leaked into argv for cwd={cwd}")

    @patch("harness.providers.gemini_oauth.subprocess.run")
    @patch("harness.providers.gemini_oauth._resolve", return_value="/fake/gemini")
    def test_prompt_goes_via_stdin_not_argv(self, _res, mrun):
        """Long prompt must NOT be on argv (Windows 8191 char limit)."""
        mrun.return_value = MagicMock(returncode=0, stdout='{"response":"ok"}', stderr="")
        long_prompt = "X" * 20_000
        GeminiOAuth().generate("sys", long_prompt)
        kwargs = mrun.call_args.kwargs
        argv = mrun.call_args[0][0]
        # The big prompt body must be the `input=...` kwarg, not in argv
        self.assertIn(long_prompt, kwargs.get("input", ""))
        for arg in argv:
            self.assertLess(len(arg), 1000, "argv arg suspiciously long — prompt leaked into argv")


# =============================================================================
# 3. INTEGRATION TESTS — real `gemini` CLI calls. Skipped if env var set.
# =============================================================================

@unittest.skipIf(SKIP_INTEGRATION, INTEGRATION_REASON)
class TestBasicGeneration(unittest.TestCase):
    """Smoke-level: can the wrapper do anything at all?"""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("gemini"):
            raise unittest.SkipTest("`gemini` CLI not on PATH")
        cls.g = GeminiOAuth()

    def test_returns_non_empty_text(self):
        r = self.g.generate("You are a calculator. Reply with only the number.",
                            "What is 6 times 7?")
        self.assertTrue(r.text.strip(), "Gemini returned empty text")
        self.assertIn("42", r.text)

    def test_token_usage_populated(self):
        r = self.g.generate("Reply with one word.", "Color of the sky?")
        self.assertGreater(r.usage.input_tokens, 0, "input_tokens should be > 0")
        self.assertGreater(r.usage.output_tokens, 0, "output_tokens should be > 0")


@unittest.skipIf(SKIP_INTEGRATION, INTEGRATION_REASON)
class TestValidatorMode(unittest.TestCase):
    """Validator role: cwd=None, no tools, must produce structured verdict."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("gemini"):
            raise unittest.SkipTest("`gemini` CLI not on PATH")
        cls.g = GeminiOAuth()

    def test_verdict_pass(self):
        sys_p = ("You are a strict code reviewer. Respond ONLY with the final "
                 "verdict line: either '判決:通過' or '判決:打回'. Nothing else.")
        usr = "Review: `def add(a,b): return a+b`. Is it correct? Verdict:"
        r = self.g.generate(sys_p, usr)
        last_line = r.text.strip().splitlines()[-1] if r.text.strip() else ""
        self.assertIn("判決", r.text, f"verdict marker missing: {r.text[:300]!r}")


@unittest.skipIf(SKIP_INTEGRATION, INTEGRATION_REASON)
class TestWorkerMode(unittest.TestCase):
    """Worker role: cwd set, approval=yolo, file/tool operations expected."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("gemini"):
            raise unittest.SkipTest("`gemini` CLI not on PATH")
        cls.g = GeminiOAuth()

    def test_write_file(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.g.generate(
                "You are a file writer. Use write_file to create the requested file. Confirm briefly.",
                "Write a file named greet.txt with content: hello world",
                cwd=td,
            )
            target = Path(td) / "greet.txt"
            self.assertTrue(target.exists(), f"file not created. Output: {r.text[:200]}")
            self.assertIn("hello world", target.read_text(encoding="utf-8"))

    def test_read_file(self):
        with tempfile.TemporaryDirectory() as td:
            secret = Path(td) / "secret.txt"
            secret.write_text("THE_PASSWORD_IS_BANANA", encoding="utf-8")
            r = self.g.generate(
                "Read the requested file using read_file and quote its contents.",
                f"Read the file secret.txt and tell me what's inside.",
                cwd=td,
            )
            self.assertIn("BANANA", r.text.upper(),
                          f"Gemini didn't read the file. Output: {r.text[:300]}")

    def test_edit_file_round_trip(self):
        """Worker reads existing file, modifies it, writes back."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.txt"
            p.write_text("level=info\ndebug=false\n", encoding="utf-8")
            self.g.generate(
                "Read the file, change `debug=false` to `debug=true`, write back. "
                "Use read_file then write_file. Confirm.",
                "Edit config.txt: set debug=true",
                cwd=td,
            )
            after = p.read_text(encoding="utf-8")
            self.assertIn("debug=true", after, f"edit not applied: {after!r}")
            self.assertNotIn("debug=false", after)

    def test_run_shell_command(self):
        """Worker can use a shell tool to inspect environment."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "marker_xyz.txt").touch()
            r = self.g.generate(
                "Use the shell tool to list files in the current directory. "
                "Then tell me whether marker_xyz.txt is there.",
                "Is marker_xyz.txt in the cwd? Use shell.",
                cwd=td,
            )
            self.assertIn("marker_xyz", r.text,
                          f"shell tool didn't find marker: {r.text[:300]}")

    def test_multi_step_read_then_write(self):
        """Realistic flow: read input, transform, write output."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "names.txt").write_text("alice\nbob\ncharlie\n", encoding="utf-8")
            self.g.generate(
                "Read names.txt, uppercase each name, write the result to names_upper.txt. "
                "Use read_file then write_file. Confirm done.",
                "Uppercase names.txt → names_upper.txt",
                cwd=td,
            )
            out = Path(td) / "names_upper.txt"
            self.assertTrue(out.exists(), "output file missing")
            content = out.read_text(encoding="utf-8")
            self.assertIn("ALICE", content)
            self.assertIn("BOB", content)
            self.assertIn("CHARLIE", content)


@unittest.skipIf(SKIP_INTEGRATION, INTEGRATION_REASON)
class TestEdgeCases(unittest.TestCase):
    """Long prompts, unicode, multiline — things Claude/Codex handle correctly."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("gemini"):
            raise unittest.SkipTest("`gemini` CLI not on PATH")
        cls.g = GeminiOAuth()

    def test_long_prompt_via_stdin(self):
        """Prompt >10KB must work (Windows argv 8191 limit dodged via stdin)."""
        filler = textwrap.dedent("""
            Background facts (ignore unless asked):
              The sky appears blue due to Rayleigh scattering of sunlight.
              Water freezes at 0°C and boils at 100°C at sea level.
        """) * 200  # ~14KB
        usr = filler + "\n\nFINAL QUESTION: Reply with exactly the word: PASS"
        r = self.g.generate("Reply with one word only.", usr)
        self.assertIn("PASS", r.text.upper(),
                      f"long-prompt round-trip failed: {r.text[:200]!r}")

    def test_unicode_prompt_and_response(self):
        """Chinese in/out — encoding=utf-8 must survive end-to-end."""
        r = self.g.generate(
            "你是翻譯助手。只回中文一個詞,不要解釋。",
            "Translate to Chinese: dog",
        )
        # Common answers: 狗 / 犬
        self.assertTrue(any(c in r.text for c in ("狗", "犬")),
                        f"Chinese reply missing: {r.text[:200]!r}")

    def test_multiline_structured_prompt(self):
        """Multiline `\\n` must survive stdin transmission."""
        usr = "Answer with the second item only.\n\nList:\n1. apple\n2. banana\n3. cherry"
        r = self.g.generate("Reply with one word.", usr)
        self.assertIn("banana", r.text.lower(),
                      f"multiline parse failed: {r.text[:200]!r}")


# =============================================================================
# Runner.
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    unit_only = "--unit" in argv
    if unit_only:
        # Force-skip integration even if env var not set
        os.environ["GEMINI_SKIP_INTEGRATION"] = "1"
        # Reload module-level flag by tweaking the unittest runner's choice
        # below (skipIf decorators read at import time so we can't undo here;
        # easier: filter by class name).
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        for cls in (TestParse, TestBuildEnv, TestDirective,
                    TestErrorHandling, TestCommandConstruction):
            suite.addTests(loader.loadTestsFromTestCase(cls))
    else:
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
