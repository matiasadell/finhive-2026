from __future__ import annotations

from databricks.sdk import WorkspaceClient

SHARED_REPO_PATH = "/Workspace/Shared/finhive-2026"
GIT_URL = "https://github.com/matiasadell/finhive-2026"
GIT_PROVIDER = "gitHub"


def main() -> None:
    w = WorkspaceClient()
    repo = next((r for r in w.repos.list() if r.path == SHARED_REPO_PATH), None)

    if repo is None:
        print(f"no Databricks Repo found at {SHARED_REPO_PATH!r}, creating it...")
        repo = w.repos.create(url=GIT_URL, provider=GIT_PROVIDER, path=SHARED_REPO_PATH)
        print(f"created Databricks Repo at {SHARED_REPO_PATH} (repo_id={repo.id})")

    w.repos.update(repo_id=repo.id, branch="main")
    print(f"synced {SHARED_REPO_PATH} to main")


if __name__ == "__main__":
    main()
