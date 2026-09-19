from __future__ import annotations

import os
from pathlib import Path

import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import JobSettings

JOBS_DIR = Path("deploy/darabricks/jobs")


def render(spec: dict, alert_email: str) -> dict:
    text = yaml.safe_dump(spec, sort_keys=False)
    text = text.replace("{alert_email}", alert_email)
    return yaml.safe_load(text)


def deploy(w: WorkspaceClient, spec: dict) -> None:
    name = spec["name"]
    settings = JobSettings.from_dict({k: v for k, v in spec.items() if k != "name"})

    existing = next(iter(w.jobs.list(name=name)), None)
    if existing:
        w.jobs.reset(job_id=existing.job_id, new_settings=JobSettings(name=name, **settings.as_dict()))
        print(f"updated job '{name}' (job_id={existing.job_id})")
    else:
        created = w.jobs.create(name=name, **settings.as_dict())
        print(f"created job '{name}' (job_id={created.job_id})")


def main() -> None:
    alert_email = os.environ.get("ALERT_EMAIL", "")
    if not alert_email:
        raise SystemExit("ALERT_EMAIL env var is required")

    w = WorkspaceClient()
    spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
    if not spec_paths:
        raise SystemExit(f"no job specs found under {JOBS_DIR}")

    for spec_path in spec_paths:
        spec = render(yaml.safe_load(spec_path.read_text()), alert_email)
        deploy(w, spec)


if __name__ == "__main__":
    main()
