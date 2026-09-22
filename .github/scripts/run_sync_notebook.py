from databricks.sdk import WorkspaceClient

NOTEBOOK_PATH = "/Workspace/Shared/finhive-2026/.github/scripts/sync_repo"


def main() -> None:
    w = WorkspaceClient()

    print(f"Running notebook {NOTEBOOK_PATH}...")
    result = w.notebooks.run(notebook_path=NOTEBOOK_PATH)

    print(f"Notebook execution completed with state: {result.state}")


if __name__ == "__main__":
    main()
