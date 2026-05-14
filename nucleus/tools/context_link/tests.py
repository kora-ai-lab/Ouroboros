import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.context_link.tool import link_context

result = link_context("test query")
assert "query" in result and "memories" in result
print("context_link tests passed")
