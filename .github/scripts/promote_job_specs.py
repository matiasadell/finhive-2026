from pathlib import Path
import yaml

JOBS_DIR = Path("deploy/darabricks/jobs")

def promote(job: dict) -> dict:
    # iterate over tasks
    for task in job.get("tasks", []):

        # change path to Shared
        notebook_task = task.get("notebook_task", {})
        if "notebook_path" in notebook_task:
            path = notebook_task["notebook_path"]
            if "finhive-2026" in path:
                notebook_task["notebook_path"] = "/Workspace/Shared/finhive-2026" + path.split("finhive-2026", 1)[1]
        
        base_parameters = notebook_task.get("base_parameters", {})
        for key, value in base_parameters.items():
            if isinstance(value, str) and "finhive-2026" in value:
                path = value
                base_parameters[key] = "/Workspace/Shared/finhive-2026" + path.split("finhive-2026", 1)[1]
    return job

# retrieve all the job definitions
spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
if not spec_paths:
    raise SystemExit(f"no job specs found under {JOBS_DIR}")

# iterate over the yamls
for spec_path in spec_paths:
    spec = yaml.safe_load(spec_path.read_text())
    # verify structure resources: jobs:
    jobs = (spec.get("resources") or {}).get("jobs") or {}
    if not jobs:
        raise SystemExit(f"no jobs found under resources.jobs in {spec_path}")

    # iterate every job in the spec. only one is expected but allows multiple
    for job_key, job in jobs.items():
        # change paths to Shared
        promote(job)

    # overwrite the job definition
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    print(f"promoted {spec_path}")
