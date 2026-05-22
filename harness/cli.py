"""Top-level `mission` command — dispatches to subcommands.

Subcommands:
    mission run <manifest> [--contract ...]   Execute a manifest
    mission console [PROJECT]                 Interactive TUI
    mission dashboard [PROJECT]               One-shot rich snapshot
    mission watch [PROJECT]                   Live-updating rich snapshot
    mission tail [PROJECT] [-n N]             Last N log events
    mission tasks [PROJECT]                   Show task table
    mission status [PROJECT]                  Show provider quota state
    mission reset [TARGET] [PROJECT]          Reset quota state

The output of `dashboard`, `tail`, `tasks`, `status` is colored but plain
text — Claude Code (or any agent) can call them via Bash and see the same
view a human sees, with no special parsing.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

# Force UTF-8 on Windows so rich's Unicode glyphs (● ◐ ✓ ✗) don't crash
# under legacy code pages (cp950 / cp1252). No-op on already-UTF-8 stdouts.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, io.UnsupportedOperation):
        pass


def _add_project_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("project", nargs="?", type=Path, default=Path.cwd(),
                   help="project directory (default: cwd)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission",
        description="mission-framework — cross-model multi-agent harness",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="execute a manifest")
    p_run.add_argument("manifest", type=Path)
    p_run.add_argument("--contract", type=Path, default=None)

    p_console = sub.add_parser("console", help="interactive TUI")
    _add_project_arg(p_console)

    p_dash = sub.add_parser("dashboard", help="one-shot rich snapshot")
    _add_project_arg(p_dash)

    p_watch = sub.add_parser("watch", help="live-updating rich snapshot")
    _add_project_arg(p_watch)
    p_watch.add_argument("--interval", type=float, default=1.0)

    p_tail = sub.add_parser("tail", help="show last N log events")
    _add_project_arg(p_tail)
    p_tail.add_argument("-n", "--num", type=int, default=30)

    p_tasks = sub.add_parser("tasks", help="show task table from manifest.json")
    _add_project_arg(p_tasks)

    p_status = sub.add_parser("status", help="show provider quota state")
    _add_project_arg(p_status)

    p_reset = sub.add_parser("reset", help="reset quota for one or all providers")
    p_reset.add_argument("target", nargs="?", default=None,
                         help="provider/model key, e.g. claude-cli/claude-opus-4-7 (omit = all)")
    _add_project_arg(p_reset)

    p_skills = sub.add_parser("skills", help="manage the skill library (~/.mission/skills/)")
    skills_sub = p_skills.add_subparsers(dest="skills_cmd")
    skills_sub.add_parser("list", help="list installed skills")
    p_skills_install = skills_sub.add_parser("install-seeds",
                                             help="copy bundled seed skills into ~/.mission/skills/")

    args = parser.parse_args(argv)

    if args.cmd is None:
        # default: show dashboard for cwd
        from . import dashboard
        dashboard.show_dashboard(Path.cwd())
        return 0

    if args.cmd == "run":
        from . import runner
        return runner.run(args.manifest, args.contract)

    if args.cmd == "console":
        from . import console
        return console.main([str(args.project)])

    if args.cmd == "dashboard":
        from . import dashboard
        dashboard.show_dashboard(args.project)
        return 0

    if args.cmd == "watch":
        from . import dashboard
        dashboard.watch_dashboard(args.project, interval=args.interval)
        return 0

    if args.cmd == "tail":
        from . import dashboard
        dashboard.tail_events(args.project, n=args.num)
        return 0

    if args.cmd == "tasks":
        from . import dashboard
        dashboard.list_tasks(args.project)
        return 0

    if args.cmd == "status":
        from . import runner
        return runner.cmd_status(args.project)

    if args.cmd == "reset":
        from . import runner
        return runner.cmd_reset(args.project, args.target)

    if args.cmd == "skills":
        from . import skills as _skills
        import shutil
        if args.skills_cmd == "list" or not args.skills_cmd:
            files = _skills.list_skills()
            if not files:
                print("(no skills installed — try `mission skills install-seeds`)")
            else:
                for f in files:
                    print(f"  {f.stem:<35}  {f.stat().st_size:>6} bytes")
            return 0
        if args.skills_cmd == "install-seeds":
            seeds_dir = Path(__file__).parent / "skills_seed"
            target = _skills.skills_dir()
            count = 0
            for seed in seeds_dir.glob("*.md"):
                dst = target / seed.name
                if dst.exists():
                    print(f"  [SKIP] {seed.name} (already exists)")
                    continue
                shutil.copy2(seed, dst)
                print(f"  [INSTALL] {seed.name}")
                count += 1
            print(f"\n{count} seed(s) installed at {target}")
            return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
