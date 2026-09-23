import os
from pathlib import Path

import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import JobSettings

JOBS_DIR = Path("deploy/darabricks/jobs")


def deploy(w: WorkspaceClient, spec: dict) -> None:
    name = spec["name"]
    settings = JobSettings.from_dict({k: v for k, v in spec.items() if k != "name"})

    existing = next(iter(w.jobs.list(name=name)), None)
    if existing:
        w.jobs.reset(job_id=existing.job_id, new_settings=JobSettings(name=name, tasks=settings))
        print(f"updated job '{name}' (job_id={existing.job_id})")
    else:
        created = w.jobs.create(name=name, **settings.as_shallow_dict())
        print(f"created job '{name}' (job_id={created.job_id})")


def main() -> None:
    host = os.environ.get("DATABRICKS_HOST")
    token = os.environ.get("DATABRICKS_TOKEN")

    if not host:
        raise SystemExit("DATABRICKS_HOST env var is required")
    if not token:
        raise SystemExit("DATABRICKS_CLIENT_SECRET env var is required")

    w = WorkspaceClient(host=host, token=token)
    spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
    if not spec_paths:
        raise SystemExit(f"no job specs found under {JOBS_DIR}")

    for spec_path in spec_paths:
        spec = yaml.safe_load(spec_path.read_text())
        deploy(w, spec)


if __name__ == "__main__":
    main()
