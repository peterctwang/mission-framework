"""Skill library — reusable patterns extracted from past missions.

Skills are markdown files under `~/.mission/skills/` that document
how to handle recurring task shapes. Examples:
    - "flask-stdlib-http-server.md" — building a tiny Flask app for serving JSON
    - "phaser-pixel-dashboard.md" — Phaser scene + sprite loading conventions
    - "claude-cli-headless-invocation.md" — the correct flag combo

The Orchestrator reads relevant skills before planning (matching on mission
keywords). Workers can request specific skills via a `## REQUEST_SKILL`
block (future).

For v1, this is a scaffold + manual curation pipeline. Future iterations
can add automatic skill extraction from successful missions.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def skills_dir() -> Path:
    """Locate the skills directory:
    1. $MISSION_SKILLS_DIR if set
    2. ~/.mission/skills/ otherwise
    Created on first access.
    """
    if env := os.environ.get("MISSION_SKILLS_DIR"):
        d = Path(env)
    else:
        d = Path.home() / ".mission" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_skills() -> list[Path]:
    """Return all .md skill files."""
    return sorted(skills_dir().glob("*.md"))


def search_skills(query: str, *, limit: int = 5) -> list[tuple[Path, int]]:
    """Cheap text-match scoring: count case-insensitive keyword hits in
    skill front-matter + body. Returns top-N (path, score) tuples."""
    query_tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    if not query_tokens:
        return []
    scored: list[tuple[Path, int]] = []
    for skill_path in list_skills():
        try:
            body = skill_path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        score = sum(body.count(tok) for tok in query_tokens)
        if score > 0:
            scored.append((skill_path, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def load_skills_for_mission(mission_desc: str, *, max_chars: int = 8000) -> str:
    """Build a context block of relevant skills for an Orchestrator prompt.
    Returns markdown ready to interpolate; empty string when nothing relevant.
    """
    matches = search_skills(mission_desc)
    if not matches:
        return ""
    chunks = ["## Relevant skills (from skill library)\n"]
    total = 0
    for path, score in matches:
        body = path.read_text(encoding="utf-8").strip()
        header = f"\n### {path.stem} (relevance={score})\n"
        chunks.append(header)
        # Truncate any one skill aggressively to keep budget
        budget = max(1500, (max_chars - total) // max(1, len(matches)))
        chunks.append(body[:budget])
        total += len(header) + min(len(body), budget)
        if total >= max_chars:
            break
    return "\n".join(chunks)


def write_skill(name: str, body: str) -> Path:
    """Persist a new skill (manual or auto-generated). Returns its path."""
    safe = re.sub(r"[^\w\-]", "-", name.strip().lower())
    p = skills_dir() / f"{safe}.md"
    p.write_text(body, encoding="utf-8")
    return p
