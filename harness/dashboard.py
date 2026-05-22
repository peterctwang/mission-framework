"""Rich-formatted dashboard views.

Two kinds of consumers:

1. **Humans in a terminal** — they run `mission dashboard` and see a
   one-shot rendered snapshot, or `mission watch` for a live-updating one.

2. **Claude Code (or any agent acting as console)** — Claude calls
   `mission dashboard` via the Bash tool. Whatever this prints is what
   Claude sees. The output is plain rendered ANSI, readable as-is — no
   special schema for Claude to parse.

Designed to be glanceable: provider health on the left, current mission on
the right, recent events at the bottom. Every layout fits in 100x40.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config
from .quota import QuotaTracker

console = Console()

# Visual cues
STATUS_GLYPH = {"ok": "●", "exhausted": "◐", "unavailable": "○"}
STATUS_COLOR = {"ok": "green", "exhausted": "yellow", "unavailable": "red"}
EVENT_COLOR = {
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
    "provider-skip-diversity": "dim",
    "cache-hit": "blue",
    "subtask-failed": "red bold",
    "skip-done": "dim",
    "run-end": "white bold",
}


def _state_dir(project: Path) -> Path:
    return project.resolve()


def _read_state(project: Path) -> QuotaTracker:
    return QuotaTracker.load(_state_dir(project) / ".harness-state.json", budgets=config.BUDGETS)


def _read_manifest(project: Path) -> dict | None:
    p = project / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            return None
    for p in project.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            continue
        if "subtasks" in data and "mission" in data:
            return data
    return None


def _read_log_tail(project: Path, n: int = 20) -> list[dict]:
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


# ---------- Panels ----------

def render_providers(tracker: QuotaTracker) -> Panel:
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
                  expand=True, pad_edge=False, padding=(0, 1))
    table.add_column("", width=2)
    table.add_column("Provider", overflow="fold")
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("Calls", justify="right")
    table.add_column("Status")

    if not tracker.providers:
        table.add_row("", Text("(no usage yet — run a mission)", style="dim"),
                      "", "", "", "")
    for key, st in sorted(tracker.providers.items()):
        glyph = STATUS_GLYPH.get(st.status, "?")
        color = STATUS_COLOR.get(st.status, "white")
        table.add_row(
            Text(glyph, style=color),
            key,
            f"{st.tokens_in:,}",
            f"{st.tokens_out:,}",
            f"{st.invocations}",
            Text(st.status, style=color),
        )
    return Panel(table, title="[bold]PROVIDERS[/]", border_style="cyan", padding=(0, 1))


def render_mission(manifest: dict | None) -> Panel:
    if not manifest:
        return Panel(Text("no manifest found in this directory", style="dim"),
                     title="[bold]MISSION[/]", border_style="magenta")
    subtasks = manifest.get("subtasks", [])
    total = len(subtasks)
    done = sum(1 for s in subtasks if s.get("status") == "done")
    in_progress = sum(1 for s in subtasks if s.get("status") == "in-progress")
    rework = sum(1 for s in subtasks if s.get("status") == "rework")
    todo = total - done - in_progress - rework

    lines = [
        Text(manifest.get("mission", "(no description)"), style="bold"),
        Text(""),
        Text.assemble(
            ("done    ", "dim"),
            (f"{done}", "green"),
            ("  ", ""),
            ("active  ", "dim"),
            (f"{in_progress}", "cyan"),
            ("  ", ""),
            ("rework  ", "dim"),
            (f"{rework}", "yellow"),
            ("  ", ""),
            ("todo    ", "dim"),
            (f"{todo}", "white"),
            ("  /  ", "dim"),
            (f"{total}", "bold"),
            (" total", "dim"),
        ),
    ]
    return Panel(Group(*lines), title="[bold]MISSION[/]", border_style="magenta", padding=(1, 2))


def render_tasks(manifest: dict | None) -> Panel:
    if not manifest:
        return Panel(Text("(no tasks)", style="dim"),
                     title="[bold]TASKS[/]", border_style="white")
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
                  expand=True, pad_edge=False, padding=(0, 1))
    table.add_column("ID", width=6)
    table.add_column("Description", overflow="fold")
    table.add_column("Diff", width=4)
    table.add_column("Profile", width=10)
    table.add_column("Validator", width=4, justify="center")
    table.add_column("Status")

    for sub in manifest.get("subtasks", []):
        status = sub.get("status", "todo")
        status_color = {
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
            Text(status, style=status_color),
        )
    return Panel(table, title="[bold]TASKS[/]", border_style="white")


def render_events(events: list[dict]) -> Panel:
    if not events:
        return Panel(Text("(no events logged yet)", style="dim"),
                     title="[bold]EVENTS[/]", border_style="yellow")
    lines = []
    for evt in events:
        ts = evt.get("ts", "")[-8:]  # HH:MM:SS
        ev = evt.get("event", "?")
        color = EVENT_COLOR.get(ev, "white")
        # Build event-specific detail
        detail_bits = []
        for k in ("id", "provider", "model", "attempt", "reason", "note", "factory"):
            if v := evt.get(k):
                detail_bits.append(f"{k}={v}")
        detail = "  ".join(detail_bits)
        lines.append(Text.assemble(
            (f"{ts}  ", "dim"),
            (f"{ev:<24}", color),
            (f"  {detail}", "dim"),
        ))
    return Panel(Group(*lines), title="[bold]EVENTS (latest)[/]",
                 border_style="yellow", padding=(0, 1))


# ---------- Top-level layouts ----------

def build_layout(project: Path) -> Layout:
    """Full-screen layout for the live `watch` mode."""
    tracker = _read_state(project)
    manifest = _read_manifest(project)
    events = _read_log_tail(project, n=18)

    layout = Layout()
    layout.split_column(
        Layout(name="top", size=10),
        Layout(name="middle"),
        Layout(name="bottom", size=14),
    )
    layout["top"].split_row(
        Layout(render_mission(manifest), name="mission"),
        Layout(render_providers(tracker), name="providers", ratio=2),
    )
    layout["middle"].update(render_tasks(manifest))
    layout["bottom"].update(render_events(events))
    return layout


def _build_snapshot_group(project: Path) -> Group:
    """Stack panels vertically; lets each size to its content (no clipping)."""
    from rich.columns import Columns
    tracker = _read_state(project)
    manifest = _read_manifest(project)
    events = _read_log_tail(project, n=18)
    header = Columns(
        [render_mission(manifest), render_providers(tracker)],
        equal=False, expand=True,
    )
    return Group(header, render_tasks(manifest), render_events(events))


def show_dashboard(project: Path) -> None:
    """One-shot snapshot. Good for Claude Code: ask Claude to run
    `mission dashboard` and the rendered output appears in the Bash tool result."""
    console.print(_build_snapshot_group(project))


def watch_dashboard(project: Path, interval: float = 1.0) -> None:
    """Live-updating full-screen dashboard. For humans in a real terminal."""
    with Live(build_layout(project), refresh_per_second=4, screen=True, console=console) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(build_layout(project))
        except KeyboardInterrupt:
            return


def tail_events(project: Path, n: int = 30) -> None:
    """Print the last N log events as a colored table.
    Designed for Claude Code: easy to glance, no need to read raw JSONL."""
    events = _read_log_tail(project, n=n)
    console.print(render_events(events))


def list_tasks(project: Path) -> None:
    manifest = _read_manifest(project)
    console.print(render_tasks(manifest))
    if manifest:
        console.print(render_mission(manifest))
