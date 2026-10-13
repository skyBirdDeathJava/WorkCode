"""Web 工具：web_search（网页搜索）+ web_fetch（抓取网页）。

- web_search：DuckDuckGo HTML 搜索（无 Key），解析标题/链接/摘要
- web_fetch：httpx 抓取，转成纯文本；含 SSRF 防护（拒绝内网/回环地址）

安全：web_search 只读允许；web_fetch 需用户确认（网关 ask），且工具内再做 SSRF 防护。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import re
import socket
from urllib.parse import parse_qs, unquote, urlparse

import httpx

MAX_SEARCH_RESULTS = 5
MAX_FETCH_CHARS = 8000
TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")

DDG_URL = "https://html.duckduckgo.com/html/"
_TITLE_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)

BING_URL = "https://www.bing.com/search"
_ALGO_RE = re.compile(r'<li class="b_algo"[\s\S]*?</li>')
_BING_HREF_RE = re.compile(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)

WIKI_URL = "https://en.wikipedia.org/w/api.php"

_SEARCH_BACKENDS = ("bing", "duckduckgo", "wikipedia")


def _clean_fragment(s: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


async def web_search(query: str, max_results: int = MAX_SEARCH_RESULTS) -> tuple[bool, str]:
    if not query or not query.strip():
        return (False, "搜索关键字为空")
    errors: list[str] = []
    for backend in _SEARCH_BACKENDS:
        try:
            ok, out = await _search_backend(backend, query.strip(), max_results)
            if ok:
                return (True, out)
            errors.append(f"{backend}: {out}")
        except Exception as e:  # noqa: BLE001 — 后端失败继续尝试下一个
            errors.append(f"{backend}: {e}")
    return (False, "所有搜索后端均失败：" + "；".join(errors[:3]))


async def _search_backend(backend: str, query: str, max_results: int) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        if backend == "bing":
            resp = await client.get(BING_URL, params={"q": query})
            return _parse_bing(resp.text, max_results)
        if backend == "duckduckgo":
            resp = await client.get(DDG_URL, params={"q": query})
            return _parse_ddg(resp.text, max_results)
        if backend == "wikipedia":
            resp = await client.get(
                WIKI_URL,
                params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": str(max_results)},
            )
            return _parse_wikipedia(resp.json(), max_results)
    return (False, "未知后端")


def _parse_bing(body: str, max_results: int) -> tuple[bool, str]:
    blocks = _ALGO_RE.findall(body)
    lines: list[str] = []
    for block in blocks[:max_results]:
        tm = _BING_HREF_RE.search(block)
        if not tm:
            continue
        url, title = tm.group(1), _clean_fragment(tm.group(2))
        snippet = ""
        pm = _BING_SNIPPET_RE.search(block)
        if pm:
            snippet = _clean_fragment(pm.group(1))
        lines.append(f"{len(lines) + 1}. {title}\n   {url}\n   {snippet[:200]}")
    if not lines:
        return (True, "（无搜索结果）")
    return (True, "\n".join(lines))


def _parse_ddg(body: str, max_results: int) -> tuple[bool, str]:
    titles = _TITLE_RE.findall(body)
    if not titles:
        return (False, "DDG 无结果（可能被反爬拦截）")
    snippets = _SNIPPET_RE.findall(body)
    lines = []
    for i, (href, title) in enumerate(titles[:max_results]):
        url = _ddg_real_url(href)
        title_text = _clean_fragment(title)
        snippet = _clean_fragment(snippets[i]) if i < len(snippets) else ""
        lines.append(f"{i + 1}. {title_text}\n   {url}\n   {snippet[:200]}")
    return (True, "\n".join(lines))


def _parse_wikipedia(data: dict, max_results: int) -> tuple[bool, str]:
    results = data.get("query", {}).get("search", [])
    if not results:
        return (False, "Wikipedia 无结果")
    lines = []
    for i, it in enumerate(results[:max_results]):
        title = it.get("title", "")
        snippet = _clean_fragment(it.get("snippet", ""))
        url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
        lines.append(f"{i + 1}. {title}\n   {url}\n   {snippet[:200]}")
    return (True, "\n".join(lines))


def _ddg_real_url(href: str) -> str:
    """DDG 的 href 是跳转链接，取其中的 uddg 参数还原真实 URL。"""
    try:
        q = urlparse(href).query
        uddg = parse_qs(q).get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    except Exception:
        pass
    return href


def _ssrf_blocked(url: str) -> bool:
    """拒绝访问内网/回环/链路本地地址（SSRF 防护）。"""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # 无法解析 → 拒绝
    for info in infos[:4]:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _html_to_text(body: str) -> str:
    body = _SCRIPT_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    body = html_lib.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip()


async def web_fetch(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (False, "只支持 http/https 链接")
    if _ssrf_blocked(url):
        return (False, "该地址被安全策略拒绝（SSRF 防护）")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return (False, f"抓取失败: {e}")

    ctype = resp.headers.get("content-type", "")
    if not (ctype.startswith("text/") or "html" in ctype):
        return (True, f"（非文本内容：{ctype}，已跳过）")

    text = _html_to_text(resp.text)
    if not text:
        return (True, "（网页无可提取文本）")
    if len(text) > MAX_FETCH_CHARS:
        text = text[:MAX_FETCH_CHARS] + "\n...(内容过长，已截断)"
    return (True, f"URL: {url}\n\n{text}")
