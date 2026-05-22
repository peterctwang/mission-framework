"""Smoke test for the FILES_TO_WRITE post-processor."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

from harness.runner import _apply_files_to_write

WORKER_OUTPUT = """## Implementation
preview...

## FILES_TO_WRITE

### hello.py
```python
# greeter
print('world')
```

### nested/dir/README.md
```markdown
Test passed.
```

### oldfile.txt (DELETE)
```
```

### ../escape.txt
```
should be rejected
```

## Handoff
Done.
"""


def log(event: str, **kw) -> None:
    print(f"  [{event}] {kw}")


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        proj = Path(t)
        (proj / "oldfile.txt").write_text("to be removed", encoding="utf-8")
        n = _apply_files_to_write(WORKER_OUTPUT, proj, log, "T-test")
        print(f"\nwrote count: {n}")
        print("tree:")
        for p in sorted(proj.rglob("*")):
            rel = p.relative_to(proj)
            if p.is_file():
                print(f"  {rel}  ({p.stat().st_size} bytes)")
        # Verify content
        assert (proj / "hello.py").read_text(encoding="utf-8") == "# greeter\nprint('world')"
        assert (proj / "nested/dir/README.md").read_text(encoding="utf-8") == "Test passed."
        assert not (proj / "oldfile.txt").exists(), "DELETE should have removed oldfile"
        assert not (proj / "../escape.txt").exists(), "traversal should be rejected"
        print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
