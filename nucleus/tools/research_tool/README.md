# research_tool

Web research tool that searches DuckDuckGo and optionally scrapes full page content.

## Usage
```json
{"query": "African AI sovereignty 2026", "sources": 5, "depth": "quick"}
```

- `depth: "quick"` → search results only (title + URL)
- `depth: "deep"` → search + scrape full page content

Requires network access (HTTPS outbound).
