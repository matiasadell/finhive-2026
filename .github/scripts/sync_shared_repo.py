from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceDoesNotExist

GIT_URL = "https://github.com/matiasadell/finhive-2026"
SHARED_REPO_PATH = f"/Workspace/Shared/{GIT_URL.split('/')[-1]}"

w = WorkspaceClient()

try:
    status = w.workspace.get_status(SHARED_REPO_PATH)
    print(status)
    repo_id = status.object_id
except ResourceDoesNotExist:
    print(f"no Git folder found at {SHARED_REPO_PATH!r}, creating it...")
    repo = w.repos.create(
        url=GIT_URL,
        provider="gitHub",
        path=SHARED_REPO_PATH
    )
    repo_id = repo.id
    print(f"created Git folder at {SHARED_REPO_PATH} (repo_id={repo_id})")

w.repos.update(repo_id=repo_id, branch="main")
print(f"synced {SHARED_REPO_PATH} to main (repo_id={repo_id})")
