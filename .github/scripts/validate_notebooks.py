from __future__ import annotations

import json
import sys
from pathlib import Path

NOTEBOOKS_DIR = Path("notebooks")


def main() -> None:
    paths = sorted(NOTEBOOKS_DIR.rglob("*.ipynb"))
    if not paths:
        sys.exit(f"no notebooks found under {NOTEBOOKS_DIR}")

    failed = False
    for path in paths:
        try:
            notebook = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            failed = True
            print(f"INVALID {path}: not valid JSON ({e})")
            continue

        if "cells" not in notebook:
            failed = True
            print(f"INVALID {path}: missing 'cells'")
            continue

        print(f"OK {path} ({len(notebook['cells'])} cells)")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
