from pathlib import Path
import yaml

JOBS_DIR = Path("deploy/darabricks/jobs")
SHARED_PREFIX = "/Workspace/Shared/finhive-2026/"


def promote(spec: dict) -> dict:
    for task in spec.get("tasks", []):
        notebook_task = task.get("notebook_task", {})
        for key in ("notebook_path",):
            if key in notebook_task:
                path = notebook_task[key]
                if "finhive-2026" in path:
                    notebook_task[key] = "/Workspace/Shared/finhive-2026" + path.split("finhive-2026", 1)[1]
        base_parameters = notebook_task.get("base_parameters", {})
        for key, value in base_parameters.items():
            if isinstance(value, str) and "finhive-2026" in value:
                path = value
                base_parameters[key] = "/Workspace/Shared/finhive-2026" + path.split("finhive-2026", 1)[1]
    return spec


spec_paths = sorted(JOBS_DIR.glob("*.yaml"))
if not spec_paths:
    raise SystemExit(f"no job specs found under {JOBS_DIR}")

for spec_path in spec_paths:
    spec = yaml.safe_load(spec_path.read_text())
    promoted = promote(spec)
    spec_path.write_text(yaml.safe_dump(promoted, sort_keys=False))
    print(f"promoted {spec_path}")
