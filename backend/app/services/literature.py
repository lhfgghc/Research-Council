from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlencode

import httpx

OPENALEX_URL = "https://api.openalex.org/works"
ARXIV_URL = "https://export.arxiv.org/api/query"
TAVILY_URL = "https://api.tavily.com/search"
DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "ResearchCouncil/0.1 (literature-check)"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


class LiteratureError(RuntimeError):
    pass


def _abstract_from_inverted(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        for offset in offsets or []:
            positions.append((int(offset), word))
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions)[:2400]


def _tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    latin = set(re.findall(r"[a-z][a-z0-9_-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    bigrams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
    return latin | bigrams


def title_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return round(len(left_tokens & right_tokens) / len(left_tokens | right_tokens), 3)


async def search_openalex(query: str, limit: int = 8) -> list[dict[str, Any]]:
    params = {
        "search": query,
        "per-page": limit,
        "select": (
            "id,doi,title,publication_year,authorships,primary_location,"
            "abstract_inverted_index,cited_by_count"
        ),
        "mailto": "research-council@example.com",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(
                OPENALEX_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
            )
        if response.status_code >= 400:
            raise LiteratureError(
                f"OpenAlex 返回 {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LiteratureError(f"无法连接 OpenAlex：{exc}") from exc

    works: list[dict[str, Any]] = []
    for item in payload.get("results", []):
        authors = [
            (authorship.get("author") or {}).get("display_name", "")
            for authorship in (item.get("authorships") or [])[:6]
        ]
        venue = ((item.get("primary_location") or {}).get("source") or {}).get(
            "display_name", ""
        )
        works.append(
            {
                "source": "OpenAlex",
                "openalex_id": item.get("id", ""),
                "doi": item.get("doi") or "",
                "url": item.get("doi") or "",
                "title": item.get("title") or "",
                "year": item.get("publication_year"),
                "venue": venue,
                "authors": [author for author in authors if author],
                "cited_by": item.get("cited_by_count", 0),
                "abstract": _abstract_from_inverted(item.get("abstract_inverted_index")),
                "similarity": 0.0,
            }
        )
    return works


def _clean_arxiv_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


async def search_arxiv(query: str, limit: int = 8) -> list[dict[str, Any]]:
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "relevance",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(
                ARXIV_URL,
                params=urlencode(params),
                headers={"User-Agent": USER_AGENT},
            )
        if response.status_code >= 400:
            raise LiteratureError(f"arXiv 返回 {response.status_code}: {response.text[:300]}")
        root = ET.fromstring(response.text)
    except httpx.HTTPError as exc:
        raise LiteratureError(f"无法连接 arXiv：{exc}") from exc
    except ET.ParseError as exc:
        raise LiteratureError(f"arXiv 返回内容无法解析：{exc}") from exc

    works: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        arxiv_id = _clean_arxiv_text(entry.findtext("atom:id", "", ARXIV_NS))
        title = _clean_arxiv_text(entry.findtext("atom:title", "", ARXIV_NS))
        summary = _clean_arxiv_text(entry.findtext("atom:summary", "", ARXIV_NS))
        published = _clean_arxiv_text(entry.findtext("atom:published", "", ARXIV_NS))
        authors = [
            _clean_arxiv_text(author.findtext("atom:name", "", ARXIV_NS))
            for author in entry.findall("atom:author", ARXIV_NS)[:6]
        ]
        doi_text = ""
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.get("title") == "doi":
                doi_text = link.get("href", "")
        works.append(
            {
                "source": "arXiv",
                "openalex_id": arxiv_id,
                "doi": doi_text,
                "url": arxiv_id or doi_text,
                "title": title,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "venue": "arXiv",
                "authors": [author for author in authors if author],
                "cited_by": 0,
                "abstract": summary[:2400],
                "similarity": 0.0,
            }
        )
    return works


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def _decode_ddg_href(href: str) -> str:
    from urllib.parse import parse_qs, unquote, urlsplit

    if "uddg=" in href:
        query = unquote(href.split("uddg=", 1)[1])
        parsed = parse_qs(query)
        return parsed.get("uddg", [query])[0]
    if href.startswith("//"):
        return f"https:{href}"
    return href


async def search_web(query: str, limit: int = 6, tavily_api_key: str = "") -> list[dict[str, Any]]:
    """General web search. Prefers Tavily when a key is configured; otherwise best-effort DuckDuckGo HTML."""
    if tavily_api_key:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.post(
                    TAVILY_URL,
                    json={
                        "api_key": tavily_api_key,
                        "query": query,
                        "max_results": limit,
                        "search_depth": "basic",
                    },
                    headers={"User-Agent": USER_AGENT},
                )
            if response.status_code >= 400:
                raise LiteratureError(
                    f"Tavily 返回 {response.status_code}: {response.text[:200]}"
                )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise LiteratureError(f"无法连接 Tavily：{exc}") from exc
        works: list[dict[str, Any]] = []
        for item in (payload.get("results") or [])[:limit]:
            works.append(
                {
                    "source": "Web/Tavily",
                    "openalex_id": "",
                    "doi": "",
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "year": None,
                    "venue": (item.get("url") or "")[:90],
                    "authors": [],
                    "cited_by": 0,
                    "abstract": (item.get("content") or "")[:1200],
                    "similarity": 0.0,
                }
            )
        return works

    # Keyless fallback: DuckDuckGo HTML endpoint. Best effort; may fail silently.
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ResearchCouncil/0.1"
            },
        ) as client:
            response = await client.post(DDG_URL, data={"q": query, "kl": "wt-wt"})
        if response.status_code >= 400:
            raise LiteratureError(f"DuckDuckGo 返回 {response.status_code}")
        html_text = response.text
    except (httpx.HTTPError, LiteratureError) as exc:
        raise LiteratureError(f"Web 检索不可用：{exc}") from exc

    anchors = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.S
    )
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html_text, re.S
    )
    works = []
    for index, (href, title) in enumerate(anchors[:limit]):
        works.append(
            {
                "source": "Web/DuckDuckGo",
                "openalex_id": "",
                "doi": "",
                "url": _decode_ddg_href(href),
                "title": _strip_tags(title),
                "year": None,
                "venue": "",
                "authors": [],
                "cited_by": 0,
                "abstract": _strip_tags(snippets[index])[:1200] if index < len(snippets) else "",
                "similarity": 0.0,
            }
        )
    return works


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", (title or "").lower())


async def check_literature(
    *, title: str, query: str, limit: int = 8, tavily_api_key: str = ""
) -> dict[str, Any]:
    merged: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    steps: list[str] = [f"检索词：{query}"]

    tasks = [
        ("OpenAlex", search_openalex(query, limit=limit)),
        ("arXiv", search_arxiv(query, limit=limit)),
        ("Web", search_web(query, limit=max(4, limit // 2), tavily_api_key=tavily_api_key)),
    ]
    results = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    def _extend(name: str, items: Any) -> None:
        if isinstance(items, BaseException):
            steps.append(f"{name} 检索失败：{items}")
            sources.append({"source": name, "count": 0, "error": str(items)[:200]})
            return
        steps.append(f"{name} 返回 {len(items)} 条")
        sources.append({"source": name, "count": len(items)})
        for work in items:
            key = _title_key(work.get("title", ""))
            if key and any(_title_key(item.get("title", "")) == key for item in merged):
                continue
            merged.append(work)

    for (name, _), result in zip(tasks, results):
        _extend(name, result)

    for work in merged:
        work["similarity"] = title_similarity(title, work.get("title", ""))
    merged.sort(key=lambda item: item["similarity"], reverse=True)
    return {
        "query": query,
        "count": len(merged),
        "sources": sources,
        "steps": steps,
        "works": merged,
        "status": "FOUND" if merged else "EMPTY",
        "note": (
            "以上为 OpenAlex、arXiv 与 Web（Tavily 或 DuckDuckGo）的最近邻检索结果，"
            "按标题词重叠排序；仅用于定位最近邻工作，不代表完整新颖性结论。"
            "最终判断仍需阅读原文并核对引用。"
        ),
    }
