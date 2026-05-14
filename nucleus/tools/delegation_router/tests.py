# delegation_router tests
import sys
from pathlib import Path

NUCLEUS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NUCLEUS_DIR))
from subagents import LEVEL_ORDER, SubagentSpec


def test_level_order():
    assert len(LEVEL_ORDER) >= 10
    assert LEVEL_ORDER[0] == "individual"
    assert LEVEL_ORDER[-1] == "universe_god"


def test_can_delegate():
    spec = SubagentSpec(goal="test", title="world_monarch", parent_task_id="test")
    assert spec.can_delegate()
    spec2 = SubagentSpec(goal="test", title="individual", parent_task_id="test")
    assert not spec2.can_delegate()


if __name__ == "__main__":
    test_level_order()
    test_can_delegate()
    print("delegation_router tests passed")
