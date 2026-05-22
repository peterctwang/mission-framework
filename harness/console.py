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
    p = project / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            return None
    for q in project.glob("*.json"):
        try:
            data = json.loads(q.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            continue
        if "subtasks" in data and "mission" in data:
            return data
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

    #top { height: 12; }
    #top > Container { border: round $primary; }

    #mission-pane { width: 40%; }
    #providers-pane { width: 60%; }

    #tasks-pane { border: round $secondary; height: 1fr; }
    #events-pane { border: round $warning; height: 16; }

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
        with Horizontal(id="top"):
            with Container(id="mission-pane"):
                yield Static("MISSION", classes="pane-title")
                yield Static("", id="mission-body")
            with Container(id="providers-pane"):
                yield Static("PROVIDERS", classes="pane-title")
                yield DataTable(id="providers-table", show_cursor=False)
        with Container(id="tasks-pane"):
            yield Static("TASKS", classes="pane-title")
            yield DataTable(id="tasks-table", show_cursor=False)
        with Container(id="events-pane"):
            yield Static("EVENTS (latest)", classes="pane-title")
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
        # state
        self._known_events_count = 0
        self.refresh_all()
        self.set_interval(REFRESH_SECONDS, self.refresh_all)

    # ----- refresh -----

    def refresh_all(self) -> None:
        self._refresh_mission()
        self._refresh_providers()
        self._refresh_tasks()
        self._refresh_events()

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
        events = _read_log_tail(self.project, LOG_TAIL_LINES)
        log = self.query_one("#events-log", RichLog)
        new = events[self._known_events_count:]
        for evt in new:
            ts = evt.get("ts", "")[-8:]
            ev = evt.get("event", "?")
            color = self._event_color(ev)
            detail_bits = []
            for k in ("id", "provider", "model", "attempt", "reason", "note"):
                if v := evt.get(k):
                    detail_bits.append(f"{k}={v}")
            detail = "  ".join(detail_bits)
            log.write(f"[dim]{ts}[/]  [{color}]{ev:<24}[/]  [dim]{detail}[/]")
        self._known_events_count = len(events)

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
