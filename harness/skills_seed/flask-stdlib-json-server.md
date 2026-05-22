# Skill — Flask stdlib JSON serving (Mission Framework dashboards)

Topics: `flask`, `dashboard`, `json-api`, `cors`, `static-files`

## Pattern

Tiny Flask server that:
- Serves an SPA HTML at `/`
- Static frontend assets at `/static/<path>`
- 3-4 JSON endpoints reading from external state files

```python
from flask import Flask, send_from_directory, jsonify
import argparse, json
from pathlib import Path

app = Flask(__name__)
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT  # overridden by --source

@app.route("/")
def index():
    return send_from_directory(ROOT / "frontend", "index.html")

@app.route("/static/<path:p>")
def static(p):
    return send_from_directory(ROOT / "frontend", p)

@app.route("/api/state")
def state():
    f = SOURCE / ".harness-state.json"
    if not f.exists():
        return jsonify({})
    try:
        return jsonify(json.loads(f.read_text(encoding="utf-8-sig")))
    except json.JSONDecodeError:
        app.logger.warning("parse error on state.json")
        return jsonify({}), 500

@app.route("/api/manifest")
def manifest():
    f = SOURCE / "manifest.json"
    if not f.exists():
        return jsonify({})
    return jsonify(json.loads(f.read_text(encoding="utf-8-sig")))

@app.route("/api/events")
def events():
    f = SOURCE / "run.log.jsonl"
    if not f.exists():
        return jsonify([])
    lines = f.read_text(encoding="utf-8-sig").splitlines()[-200:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return jsonify(out)

@app.route("/health")
def health():
    return jsonify({"ok": True})


def main():
    global SOURCE
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=ROOT)
    p.add_argument("--port", type=int, default=19002)
    args = p.parse_args()
    if not args.source.exists():
        print(f"--source {args.source} not found", flush=True)
        raise SystemExit(2)
    SOURCE = args.source
    app.run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
```

## Gotchas

- Use `utf-8-sig` to read JSON files — PowerShell can prepend BOM
- Return `{}` (not 404) when state file is missing — frontend handles empty state
- Tail `run.log.jsonl` to last 200 lines or fetches get slow
- Avoid third-party deps beyond Flask itself (no requests/aiohttp)
- Bind to 127.0.0.1 only (no remote access)
