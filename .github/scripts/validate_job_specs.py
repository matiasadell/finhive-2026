from __future__ import annotations

import sys
from pathlib import Path

import yaml

JOBS_DIR = Path("deploy/darabricks/jobs")
REQUIRED_JOB_FIELDS = {"name", "tasks"}


def validate(job_key: str, job: dict) -> list[str]:
    errors = []

    missing = REQUIRED_JOB_FIELDS - job.keys()
    if missing:
        errors.append(f"job '{job_key}' missing field(s): {sorted(missing)}")

    tasks = job.get("tasks") or []
    if not tasks:
        errors.append(f"job '{job_key}' must define at least one task")

    for task in tasks:
        task_key = task.get("task_key")
        if not task_key:
            errors.append(f"job '{job_key}' has a task missing 'task_key'")
            continue

        notebook_task = task.get("notebook_task")
        if not notebook_task:
            errors.append(f"job '{job_key}' task '{task_key}' is missing 'notebook_task'")
            continue

        path = notebook_task.get("notebook_path", "")
        if "finhive-2026" not in path:
            errors.append(
                f"job '{job_key}' task '{task_key}' notebook_path must reference "
                f"finhive-2026, got {path!r}"
            )

    return errors


spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
if not spec_paths:
    sys.exit(f"no job specs found under {JOBS_DIR}")

failed = False
for spec_path in spec_paths:
    spec = yaml.safe_load(spec_path.read_text())
    jobs = (spec.get("resources") or {}).get("jobs") or {}
    if not jobs:
        failed = True
        print(f"INVALID {spec_path}:")
        print("  - no jobs found under resources.jobs")
        continue

    errors = []
    for job_key, job in jobs.items():
        errors.extend(validate(job_key, job))

    if errors:
        failed = True
        print(f"INVALID {spec_path}:")
        for error in errors:
            print(f"  - {error}")
    else:
        print(f"OK {spec_path}")

if failed:
    sys.exit(1)
