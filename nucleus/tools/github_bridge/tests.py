import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.github_bridge.tool import _headers

headers = _headers()
assert "Accept" in headers
assert "User-Agent" in headers
print("github_bridge tests passed")
