from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

NUCLEUS_DIR = Path(__file__).resolve().parents[2]
if str(NUCLEUS_DIR) not in sys.path:
    sys.path.insert(0, str(NUCLEUS_DIR))

from mcp_adapter import dispatch


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    result = asyncio.run(dispatch(**payload))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
