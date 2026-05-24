"""Textual interactive console.

Run: `mission console [PROJECT_DIR]`

Keyboard:
  r     reset provider quotas (modal)
  s     submit / run a manifest (modal)
  e     refresh now (otherwise auto-refresh ~1Hz)
  q     quit

Panels read live from `<project>/.harness-state.json`, `manifest.json`, and
`run.log.jsonl` — same files the headless runner writes to. The console
never owns state; it just visualizes.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from . import config
from .quota import QuotaTracker

REFRESH_SECONDS = 1.0
LOG_TAIL_LINES = 50

STATUS_GLYPH = {"ok": "●", "exhausted": "◐", "unavailable": "○"}
STATUS_STYLE = {"ok": "green", "exhausted": "yellow", "unavailable": "red"}


def _read_state(project: Path) -> QuotaTracker:
    return QuotaTracker.load(project / ".harness-state.json", budgets=config.BUDGETS)


def _read_manifest(project: Path) -> dict | None:
    """Pick the most recently modified manifest-shaped JSON file in project.

    This auto-tracks the active mission even when you maintain multiple
    manifest-*.json files in one project dir (e.g. v1 + visual-upgrade).
    Falls back to manifest.json by name if no candidate has 'subtasks'.
    """
    candidates: list[tuple[float, dict]] = []
    for q in project.glob("*.json"):
        try:
            data = json.loads(q.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and "subtasks" in data and "mission" in data:
            candidates.append((q.stat().st_mtime, data))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return None


def _read_log_tail(project: Path, n: int) -> list[dict]:
    log_path = project / "run.log.jsonl"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


class ResetScreen(ModalScreen[str | None]):
    """Pick a provider to reset (or 'all')."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    ResetScreen { align: center middle; }
    ResetScreen > #dialog {
        width: 60; height: auto; padding: 1 2;
        border: thick $accent; background: $panel;
    }
    ResetScreen Button { margin-top: 1; }
    """

    def __init__(self, providers: list[str]):
        super().__init__()
        self._providers = providers

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Reset provider quota[/b]")
            yield Label("")
            yield Button("Reset ALL providers", id="reset-all", variant="warning")
            for prov in self._providers:
                yield Button(f"Reset  {prov}", id=f"reset-{prov}")
            yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "reset-all":
            self.dismiss("__all__")
        elif event.button.id and event.button.id.startswith("reset-"):
            self.dismiss(event.button.id[len("reset-"):])


class RunScreen(ModalScreen[Path | None]):
    """Pick a manifest path and launch a mission run."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    RunScreen { align: center middle; }
    RunScreen > #dialog {
        width: 70; height: auto; padding: 1 2;
        border: thick $accent; background: $panel;
    }
    RunScreen Input { margin: 1 0; }
    """

    def __init__(self, default_path: Path):
        super().__init__()
        self._default = default_path

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Run a mission[/b]")
            yield Label("Path to manifest.json (relative or absolute):")
            yield Input(value=str(self._default), id="manifest-path")
            with Horizontal():
                yield Button("Run", id="run", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        path = Path(self.query_one("#manifest-path", Input).value).expanduser().resolve()
        if not path.exists():
            self.notify(f"file not found: {path}", severity="error")
            return
        self.dismiss(path)


class ConsoleApp(App):
    """Mission Framework interactive console."""

    CSS = """
    Screen { background: $surface; }

    /* Top strip: mission summary + providers */
    #top { height: 11; }
    #top > Container { border: round $primary; }
    #mission-pane { width: 38%; }
    #providers-pane { width: 62%; }

    /* Mission Control split:
       Left  = TASKS list (feature/subtask table)
       Mid   = latest VALIDATOR artifact (full content)
       Right = EVENT stream */
    #control-row { height: 1fr; }
    #tasks-pane     { border: round $secondary; width: 38%; }
    #validator-pane { border: round $success;   width: 32%; }
    #events-pane    { border: round $warning;   width: 30%; }

    .pane-title { background: $primary 40%; color: $text; padding: 0 1; }
    DataTable > .datatable--header { background: $primary 30%; }
    """

    BINDINGS = [
        Binding("r", "reset_provider", "Reset"),
        Binding("s", "submit_run", "Submit/Run"),
        Binding("e", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    project: reactive[Path] = reactive(Path.cwd())

    def __init__(self, project: Path):
        super().__init__()
        self.project = project.resolve()

    # ----- compose -----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Top strip — Mission summary + Providers
        with Horizontal(id="top"):
            with Container(id="mission-pane"):
                yield Static("MISSION", classes="pane-title")
                yield Static("", id="mission-body")
            with Container(id="providers-pane"):
                yield Static("PROVIDERS", classes="pane-title")
                yield DataTable(id="providers-table", show_cursor=False)
        # Mission Control row — Tasks / Validator artifact / Events
        with Horizontal(id="control-row"):
            with Container(id="tasks-pane"):
                yield Static("TASKS", classes="pane-title")
                yield DataTable(id="tasks-table", show_cursor=False)
            with Container(id="validator-pane"):
                yield Static("LATEST VALIDATOR OUTPUT", classes="pane-title")
                yield RichLog(id="validator-log", highlight=True, wrap=True, markup=False, auto_scroll=False)
            with Container(id="events-pane"):
                yield Static("EVENTS", classes="pane-title")
                yield RichLog(id="events-log", highlight=True, wrap=False, markup=True, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "mission-framework console"
        self.sub_title = str(self.project)
        # init tables
        ptable = self.query_one("#providers-table", DataTable)
        ptable.add_columns("", "Provider", "In", "Out", "Calls", "Status")
        ttable = self.query_one("#tasks-table", DataTable)
        ttable.add_columns("ID", "Description", "Diff", "Profile", "V", "Status")
        # Track total log lines we've already rendered. Using absolute line
        # number means new events are detected correctly even after the
        # tail-window slides past LOG_TAIL_LINES.
        self._last_event_line_no = -1   # -1 = first refresh shows the tail
        self.refresh_all()
        self.set_interval(REFRESH_SECONDS, self.refresh_all)

    # ----- refresh -----

    def refresh_all(self) -> None:
        self._refresh_mission()
        self._refresh_providers()
        self._refresh_tasks()
        self._refresh_events()
        self._refresh_validator()

    def _refresh_validator(self) -> None:
        """Show the most recent validator artifact's content."""
        artifacts = self.project / "artifacts"
        if not artifacts.exists():
            return
        validator_files = sorted(
            artifacts.glob("*.validator.*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        log = self.query_one("#validator-log", RichLog)
        if not validator_files:
            return
        latest = validator_files[0]
        # Track which one is shown so we don't redraw constantly.
        if getattr(self, "_shown_validator", None) == latest:
            return
        self._shown_validator = latest
        log.clear()
        log.write(f"== {latest.name} ==\n")
        body = latest.read_text(encoding="utf-8")
        # Cap to keep render cheap
        for line in body.splitlines()[:200]:
            log.write(line)

    def _refresh_mission(self) -> None:
        m = _read_manifest(self.project)
        body = self.query_one("#mission-body", Static)
        if not m:
            body.update("[dim]no manifest found in this directory[/dim]")
            return
        subs = m.get("subtasks", [])
        total = len(subs)
        done = sum(1 for s in subs if s.get("status") == "done")
        active = sum(1 for s in subs if s.get("status") == "in-progress")
        rework = sum(1 for s in subs if s.get("status") == "rework")
        todo = total - done - active - rework
        body.update(
            f"[b]{m.get('mission', '(no description)')}[/b]\n\n"
            f"  [green]done[/] {done}    "
            f"[cyan]active[/] {active}    "
            f"[yellow]rework[/] {rework}    "
            f"[white]todo[/] {todo}\n"
            f"  [dim]{done}/{total} complete[/dim]"
        )

    def _refresh_providers(self) -> None:
        tracker = _read_state(self.project)
        table = self.query_one("#providers-table", DataTable)
        table.clear()
        if not tracker.providers:
            table.add_row("", "(no usage yet)", "-", "-", "-", "-")
            return
        for key, st in sorted(tracker.providers.items()):
            glyph = STATUS_GLYPH.get(st.status, "?")
            style = STATUS_STYLE.get(st.status, "white")
            table.add_row(
                f"[{style}]{glyph}[/]",
                key,
                f"{st.tokens_in:,}",
                f"{st.tokens_out:,}",
                str(st.invocations),
                f"[{style}]{st.status}[/]",
            )

    def _refresh_tasks(self) -> None:
        m = _read_manifest(self.project)
        table = self.query_one("#tasks-table", DataTable)
        table.clear()
        if not m:
            table.add_row("-", "(no tasks)", "-", "-", "-", "-")
            return
        for sub in m.get("subtasks", []):
            status = sub.get("status", "todo")
            style = {
                "done": "green",
                "in-progress": "cyan bold",
                "rework": "yellow bold",
                "todo": "dim",
            }.get(status, "white")
            table.add_row(
                sub.get("id", "?"),
                sub.get("desc", ""),
                sub.get("difficulty", ""),
                sub.get("default_profile", ""),
                "✓" if sub.get("needs_validator") else "",
                f"[{style}]{status}[/]",
            )

    def _refresh_events(self) -> None:
        """Tail run.log.jsonl by absolute line number — slides correctly
        once the log exceeds LOG_TAIL_LINES, unlike the old tail-window
        comparison which got stuck once `len(tail) == LOG_TAIL_LINES`."""
        log_path = self.project / "run.log.jsonl"
        if not log_path.exists():
            return
        lines = log_path.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        if self._last_event_line_no < 0:
            # First refresh — show the tail (last LOG_TAIL_LINES events).
            start = max(0, total - LOG_TAIL_LINES)
        else:
            start = self._last_event_line_no
        if start >= total:
            return  # nothing new

        log = self.query_one("#events-log", RichLog)
        for raw in lines[start:]:
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = evt.get("ts", "")[-8:]
            ev = evt.get("event", "?")
            color = self._event_color(ev)
            detail_bits = []
            for k in ("id", "provider", "model", "attempt", "reason", "note"):
                if v := evt.get(k):
                    detail_bits.append(f"{k}={v}")
            detail = "  ".join(detail_bits)
            log.write(f"[dim]{ts}[/]  [{color}]{ev:<24}[/]  [dim]{detail}[/]")
        self._last_event_line_no = total

    @staticmethod
    def _event_color(ev: str) -> str:
        return {
            "worker-start": "cyan",
            "worker-done": "cyan",
            "worker-retry-done": "cyan",
            "validator-start": "magenta",
            "validator-done": "magenta",
            "validator-pass": "green",
            "validator-reject": "red",
            "escalate": "yellow",
            "provider-exhausted": "red bold",
            "provider-skip-exhausted": "yellow",
            "cache-hit": "blue",
            "subtask-failed": "red bold",
        }.get(ev, "white")

    # ----- actions -----

    def action_refresh(self) -> None:
        self.refresh_all()
        self.notify("refreshed")

    def action_reset_provider(self) -> None:
        tracker = _read_state(self.project)
        providers = sorted(tracker.providers.keys()) if tracker.providers else []
        # push_screen with callback — works from any context. Don't use
        # push_screen_wait here: that requires running inside a Textual @work
        # which actions are not.
        self.push_screen(ResetScreen(providers), self._on_reset_chosen)

    def _on_reset_chosen(self, choice: str | None) -> None:
        if choice is None:
            return
        tracker = _read_state(self.project)
        if choice == "__all__":
            tracker.reset()
            self.notify("reset ALL providers", severity="warning")
        else:
            name, _, model = choice.partition("/")
            tracker.reset(name, model or None)
            self.notify(f"reset {choice}", severity="warning")
        self.refresh_all()

    def action_submit_run(self) -> None:
        default = self.project / "manifest.json"
        self.push_screen(RunScreen(default), self._on_run_chosen)

    def _on_run_chosen(self, path: "Path | None") -> None:
        if path is None:
            return
        self.notify(f"launching: {path.name}", severity="information")
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        subprocess.Popen(
            [sys.executable, "-m", "harness.runner", str(path)],
            cwd=str(self.project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Mission Framework — interactive TUI console")
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd(),
                    help="project directory (default: cwd)")
    args = ap.parse_args(argv)
    ConsoleApp(args.project).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
