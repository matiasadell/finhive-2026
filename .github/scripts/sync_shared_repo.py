from __future__ import annotations

from databricks.sdk import WorkspaceClient

SHARED_REPO_PATH = "/Workspace/Shared/finhive-2026"


def main() -> None:
    w = WorkspaceClient()
    repo = next((r for r in w.repos.list() if r.path == SHARED_REPO_PATH), None)
    if repo is None:
        raise SystemExit(
            f"no Databricks Repo found at {SHARED_REPO_PATH!r} - it must be created once "
            "by hand before CI/CD can keep it in sync (ARCHITECTURE_V2.md #7.1)"
        )

    w.repos.update(repo_id=repo.id, branch="main")
    print(f"synced {SHARED_REPO_PATH} to main")


if __name__ == "__main__":
    main()
