from __future__ import annotations

import json, sys, urllib.request, urllib.parse, urllib.error, re, html
from pathlib import Path


def _fetch(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Ouroboros/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    html_content = _fetch(url)
    results: list[dict] = []
    for match in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html_content, re.DOTALL):
        if len(results) >= max_results:
            break
        link = html.unescape(match.group(1))
        title = re.sub(r"<.*?>", "", match.group(2)).strip()
        if link and title:
            results.append({"title": title, "url": link, "source": "duckduckgo"})
    return results


def _scrape_page(url: str, max_chars: int = 5000) -> str:
    try:
        text = _fetch(url, timeout=8)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[scrape failed: {e}]"


def research(query: str, max_sources: int = 5, deep: bool = False) -> dict:
    results = _search_duckduckgo(query, max_results=max_sources)
    sources = []
    for r in results:
        entry = {"title": r["title"], "url": r["url"]}
        if deep:
            entry["content"] = _scrape_page(r["url"])
        sources.append(entry)
    return {"query": query, "sources_count": len(sources), "sources": sources}


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    result = research(
        query=str(args.get("query", "")),
        max_sources=int(args.get("sources", 5)),
        deep=str(args.get("depth", "quick")) == "deep",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
