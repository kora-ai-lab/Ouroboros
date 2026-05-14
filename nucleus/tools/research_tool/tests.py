import sys, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from tools.research_tool.tool import research

result = research("test query", max_sources=2)
assert "query" in result
assert "sources" in result
print("research_tool tests passed")
