"""Validate config/data_ingestion/*.json against the shape the ingestion orchestrator
notebooks expect: a JSON array of {"series": str, "start_date": str | null}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_DIR = Path("config/data_ingestion")


def validate(path: Path) -> list[str]:
    errors = []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [f"not valid JSON: {e}"]

    if not isinstance(data, list):
        return ["top-level value must be a JSON array"]

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            errors.append(f"entry {i} is not an object: {entry!r}")
            continue
        if not entry.get("series"):
            errors.append(f"entry {i} is missing a non-empty 'series'")
        if "start_date" not in entry:
            errors.append(f"entry {i} is missing 'start_date' (use null for full history)")
        elif entry["start_date"] is not None and not isinstance(entry["start_date"], str):
            errors.append(f"entry {i} 'start_date' must be a string or null: {entry['start_date']!r}")

    return errors


def main() -> None:
    paths = sorted(CONFIG_DIR.glob("*.json"))
    if not paths:
        sys.exit(f"no ingestion config files found under {CONFIG_DIR}")

    failed = False
    for path in paths:
        errors = validate(path)
        if errors:
            failed = True
            print(f"INVALID {path}:")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {path}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
