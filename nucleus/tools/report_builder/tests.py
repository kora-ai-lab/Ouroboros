import sys, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.report_builder.tool import build_report, ARTIFACTS_DIR

tmp = Path(tempfile.mkdtemp())
import tools.report_builder.tool as rb
rb.ARTIFACTS_DIR = tmp

result = build_report("Test Report", [{"heading": "Intro", "content": "Hello"}])
assert result["title"] == "Test Report"
assert tmp.joinpath(result["path"].split("/")[-1]).exists()
print("report_builder tests passed")
