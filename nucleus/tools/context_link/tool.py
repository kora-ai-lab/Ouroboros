from __future__ import annotations

import json, sqlite3, sys
from pathlib import Path

NUCLEUS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NUCLEUS_DIR))
from server import connect_db, retrieve_relevant, extract_keywords


def link_context(query: str, project: str | None = None) -> dict:
    results = {"query": query, "project": project, "memories": [], "sessions": [], "tasks": []}
    memories = retrieve_relevant(query)
    if memories:
        results["memories"] = [memories[:1000]]
    with connect_db() as conn:
        if project:
            rows = conn.execute(
                "SELECT session_id, summary FROM episodic_memory WHERE summary LIKE ? ORDER BY created_at DESC LIMIT 5",
                (f"%{project}%",),
            ).fetchall()
            for row in rows:
                results["sessions"].append({"id": row["session_id"], "summary": row["summary"][:200]})
        kw = extract_keywords(query)
        for word in kw[:3]:
            rows2 = conn.execute(
                "SELECT session_id, summary FROM episodic_memory WHERE keywords LIKE ? ORDER BY created_at DESC LIMIT 3",
                (f"%{word}%",),
            ).fetchall()
            for row in rows2:
                sid = row["session_id"]
                if not any(s["id"] == sid for s in results["sessions"]):
                    results["sessions"].append({"id": sid, "summary": row["summary"][:200]})
    results["count"] = len(results["sessions"]) + len(results["memories"])
    return results


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    result = link_context(
        query=str(args.get("query", "")),
        project=args.get("project"),
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
