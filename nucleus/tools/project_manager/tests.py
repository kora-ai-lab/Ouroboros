import sys, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.project_manager.tool import list_projects, add_project

data_dir = Path(tempfile.mkdtemp())
import tools.project_manager.tool as pm
pm.WORKSPACE_FILE = data_dir / "workspace.json"

result = list_projects()
assert result["projects"] == []

result = add_project("test-proj", str(data_dir))
assert result["added"] == "test-proj"

result = list_projects()
assert "test-proj" in result["projects"]

print("project_manager tests passed")
