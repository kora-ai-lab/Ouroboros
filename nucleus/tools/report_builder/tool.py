from __future__ import annotations

import json, sys, time
from pathlib import Path

try:
    _home = Path.home()
except (RuntimeError, OSError):
    _home = Path.cwd()
ARTIFACTS_DIR = _home / ".ouroboros" / "artifacts"


def build_report(title: str, sections: list[dict], fmt: str = "markdown") -> dict:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = title.lower().replace(" ", "_")[:40]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{slug}.{fmt}"
    path = ARTIFACTS_DIR / filename

    if fmt == "json":
        content = json.dumps({"title": title, "sections": sections}, indent=2)
    elif fmt == "html":
        parts = [f"<h1>{title}</h1>"]
        for s in sections:
            parts.append(f"<h2>{s.get('heading', '')}</h2><p>{s.get('content', '')}</p>")
        content = "<html><body>" + "".join(parts) + "</body></html>"
    else:
        parts = [f"# {title}"]
        for s in sections:
            parts.append(f"## {s.get('heading', '')}\n{s.get('content', '')}")
        content = "\n\n".join(parts)

    path.write_text(content, encoding="utf-8")
    return {"title": title, "path": str(path), "format": fmt, "size": len(content)}


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    result = build_report(
        title=str(args.get("title", "Untitled")),
        sections=args.get("sections", []),
        fmt=str(args.get("format", "markdown")),
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
