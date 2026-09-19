"""Rewrite every job spec's notebook_path from a personal Repos checkout to the shared
one. Run first, before anything is deployed (ARCHITECTURE_V2.md #6.2, #8.2) - the
Databricks API must only ever see job definitions that already point at
/Workspace/Shared/finhive-2026/.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

JOBS_DIR = Path("deploy/darabricks/jobs")
USER_PREFIX = re.compile(r"^/Workspace/Users/[^/]+/finhive-2026/")
SHARED_PREFIX = "/Workspace/Shared/finhive-2026/"


def promote(spec: dict) -> dict:
    for task in spec.get("tasks", []):
        notebook_task = task.get("notebook_task", {})
        for key in ("notebook_path",):
            if key in notebook_task:
                notebook_task[key] = USER_PREFIX.sub(SHARED_PREFIX, notebook_task[key])
        base_parameters = notebook_task.get("base_parameters", {})
        for key, value in base_parameters.items():
            if isinstance(value, str):
                base_parameters[key] = USER_PREFIX.sub(SHARED_PREFIX, value)
    return spec


def main() -> None:
    spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
    if not spec_paths:
        raise SystemExit(f"no job specs found under {JOBS_DIR}")

    for spec_path in spec_paths:
        spec = yaml.safe_load(spec_path.read_text())
        promoted = promote(spec)
        spec_path.write_text(yaml.safe_dump(promoted, sort_keys=False))
        print(f"promoted {spec_path}")


if __name__ == "__main__":
    main()
