"""Structural validation of every deploy/darabricks/jobs/*.yaml spec.

Run on every pull request (ci.yml) - the check that replaces `databricks bundle validate`
for job definitions deployed directly through the SDK instead of an Asset Bundle
(ARCHITECTURE_V2.md #8.1).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

JOBS_DIR = Path("deploy/darabricks/jobs")
REQUIRED_TOP_LEVEL = {"name", "tasks", "email_notifications"}


def validate(spec: dict) -> list[str]:
    errors = []

    missing = REQUIRED_TOP_LEVEL - spec.keys()
    if missing:
        errors.append(f"missing top-level field(s): {sorted(missing)}")

    tasks = spec.get("tasks") or []
    if not tasks:
        errors.append("must define at least one task")

    for task in tasks:
        task_key = task.get("task_key")
        if not task_key:
            errors.append("task is missing 'task_key'")
            continue

        notebook_task = task.get("notebook_task")
        if not notebook_task:
            errors.append(f"task '{task_key}' is missing 'notebook_task'")
            continue

        path = notebook_task.get("notebook_path", "")
        if not path.startswith("/Workspace/"):
            errors.append(
                f"task '{task_key}' notebook_path must be an absolute Workspace path, got {path!r}"
            )

    on_failure = (spec.get("email_notifications") or {}).get("on_failure")
    if not on_failure:
        errors.append("email_notifications.on_failure must list at least one recipient")

    return errors


def main() -> None:
    spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
    if not spec_paths:
        sys.exit(f"no job specs found under {JOBS_DIR}")

    failed = False
    for spec_path in spec_paths:
        spec = yaml.safe_load(spec_path.read_text())
        errors = validate(spec)
        if errors:
            failed = True
            print(f"INVALID {spec_path}:")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {spec_path}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
