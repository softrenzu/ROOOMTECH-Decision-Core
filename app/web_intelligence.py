from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import defaultdict
from html.parser import HTMLParser
from typing import Any
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

from app.semantic_matrix import SemanticItem, SemanticMatrixEngine, SemanticMatrixRequest


class WebsiteAuditRequest(BaseModel):
    start_url: str = Field(min_length=8, max_length=2048)
    max_pages: int = Field(default=250, ge=1, le=1000)
    concurrency: int = Field(default=8, ge=1, le=32)
    request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    max_page_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
    respect_robots_txt: bool = True
    include_query_strings: bool = False
    suggestions_per_page: int = Field(default=5, ge=1, le=25)
    min_semantic_score: float = Field(default=0.18, ge=0.0, le=1.0)


class WebsitePage(BaseModel):
    url: str
    status_code: int
    content_type: str | None = None
    title: str | None = None
    h1: str | None = None
    word_count: int = 0
    internal_links: int = 0
    external_links: int = 0
    incoming_internal_links: int = 0
    truncated: bool = False


class BrokenInternalLink(BaseModel):
    source_url: str
    target_url: str
    status_code: int | None = None
    error: str | None = None


class InternalLinkSuggestion(BaseModel):
    source_url: str
    target_url: str
    score: float
    suggested_anchor: str
    anchor_text_present: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class WebsiteAuditResponse(BaseModel):
    start_url: str
    pages_discovered: int
    pages_crawled: int
    html_pages: int
    failed_pages: int
    internal_links_observed: int
    external_links_observed: int
    orphan_pages: list[str]
    broken_internal_links: list[BrokenInternalLink]
    internal_link_suggestions: list[InternalLinkSuggestion]
    pages: list[WebsitePage]
    logical_semantic_pairs: int
    semantic_candidate_pairs_scored: int
    latency_ms: float
    warnings: list[str] = Field(default_factory=list)
    persistence: str = "none"


class _ParsedHTML:
    def __init__(self, title: str, h1: str, text: str, links: list[str]):
        self.title = title
        self.h1 = h1
        self.text = text
        self.links = links


class _HTMLCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._title_parts: list[str] = []
        self._h1_parts: list[str] = []
        self._text_parts: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._blocked_depth += 1
            return
        if self._blocked_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self.links.append(value.strip())
                    break

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            if self._blocked_depth:
                self._blocked_depth -= 1
            return
        if self._blocked_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str):
        if self._blocked_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self._text_parts.append(value)
        if self._in_title:
            self._title_parts.append(value)
        if self._in_h1:
            self._h1_parts.append(value)

    def parsed(self) -> _ParsedHTML:
        return _ParsedHTML(
            title=" ".join(self._title_parts).strip()[:500],
            h1=" ".join(self._h1_parts).strip()[:500],
            text=" ".join(self._text_parts).strip(),
            links=self.links,
        )


class _FetchResult:
    def __init__(
        self,
        url: str,
        status_code: int,
        content_type: str | None,
        parsed: _ParsedHTML | None,
        truncated: bool = False,
        error: str | None = None,
    ):
        self.url = url
        self.status_code = status_code
        self.content_type = content_type
        self.parsed = parsed
        self.truncated = truncated
        self.error = error


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _canonicalize(url: str, include_query_strings: bool) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    query = parts.query if include_query_strings else ""
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), path, query, ""))


def _same_origin(url: str, base_origin: tuple[str, str, int]) -> bool:
    try:
        return _origin(url) == base_origin
    except ValueError:
        return False


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return bool(ip.is_global)


async def _assert_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if not parts.hostname:
        raise ValueError("URL hostname is required")
    if parts.username or parts.password:
        raise ValueError("URLs containing credentials are not allowed")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError("only ports 80 and 443 are allowed")

    host = parts.hostname
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("private, loopback, link-local and reserved IPs are not allowed")
        return
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise

    def resolve() -> list[str]:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return sorted({row[4][0] for row in answers})

    try:
        addresses = await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise ValueError(f"hostname could not be resolved: {host}") from exc
    if not addresses:
        raise ValueError(f"hostname could not be resolved: {host}")
    if any(not _is_public_ip(address) for address in addresses):
        raise ValueError("hostname resolves to a non-public address")


def _decode_html(content: bytes, response: httpx.Response) -> str:
    encoding = response.encoding or "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


class WebsiteAuditEngine:
    """Same-origin website crawler plus local internal-link opportunity analysis.

    The crawler rejects private/reserved destinations, follows only same-origin redirects,
    does not execute JavaScript, and never persists fetched page bodies. Semantic link
    suggestions are computed locally by the ROOOMTECH sparse Unicode n-gram matrix.
    """

    user_agent = "ROOOMTECH-Decision-Core-WebAudit/0.14"

    def __init__(self):
        self.matrix = SemanticMatrixEngine()

    async def _fetch_robots(
        self,
        client: httpx.AsyncClient,
        start_url: str,
        timeout: float,
    ) -> robotparser.RobotFileParser | None:
        parts = urlsplit(start_url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            await _assert_public_url(robots_url)
            response = await client.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=timeout,
                follow_redirects=False,
            )
            if response.status_code >= 400:
                return None
            parser = robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            return parser
        except Exception:
            return None

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        base_origin: tuple[str, str, int],
        request: WebsiteAuditRequest,
    ) -> _FetchResult:
        current = url
        try:
            for _ in range(4):
                await _assert_public_url(current)
                if not _same_origin(current, base_origin):
                    return _FetchResult(current, 0, None, None, error="cross-origin redirect rejected")
                async with client.stream(
                    "GET",
                    current,
                    headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"},
                    timeout=request.request_timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return _FetchResult(current, response.status_code, response.headers.get("content-type"), None, error="redirect without location")
                        redirected = _canonicalize(urljoin(current, location), request.include_query_strings)
                        if not _same_origin(redirected, base_origin):
                            return _FetchResult(current, response.status_code, response.headers.get("content-type"), None, error="cross-origin redirect rejected")
                        current = redirected
                        continue

                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower() or None
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        remaining = request.max_page_bytes - size
                        if remaining <= 0:
                            truncated = True
                            break
                        chunks.append(chunk[:remaining])
                        size += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    parsed = None
                    if response.status_code < 400 and content_type in {"text/html", "application/xhtml+xml", None}:
                        collector = _HTMLCollector()
                        collector.feed(_decode_html(b"".join(chunks), response))
                        parsed = collector.parsed()
                    return _FetchResult(
                        current,
                        response.status_code,
                        content_type,
                        parsed,
                        truncated=truncated,
                    )
            return _FetchResult(current, 0, None, None, error="too many redirects")
        except Exception as exc:
            return _FetchResult(current, 0, None, None, error=f"{type(exc).__name__}: {exc}")

    async def audit(self, request: WebsiteAuditRequest) -> WebsiteAuditResponse:
        started = time.perf_counter()
        await _assert_public_url(request.start_url)
        start = _canonicalize(request.start_url, request.include_query_strings)
        base_origin = _origin(start)
        warnings: list[str] = []
        limits = httpx.Limits(max_connections=request.concurrency, max_keepalive_connections=request.concurrency)
        async with httpx.AsyncClient(limits=limits, trust_env=False) as client:
            robots = None
            if request.respect_robots_txt:
                robots = await self._fetch_robots(client, start, request.request_timeout_seconds)

            visited: set[str] = set()
            queued: set[str] = {start}
            pending: list[str] = [start]
            fetches: dict[str, _FetchResult] = {}
            graph: dict[str, set[str]] = defaultdict(set)
            external_counts: dict[str, int] = defaultdict(int)

            while pending and len(visited) < request.max_pages:
                room = request.max_pages - len(visited)
                batch = pending[: min(request.concurrency, room)]
                pending = pending[len(batch) :]
                allowed: list[str] = []
                for url in batch:
                    if url in visited:
                        continue
                    if robots is not None and not robots.can_fetch(self.user_agent, url):
                        visited.add(url)
                        fetches[url] = _FetchResult(url, 0, None, None, error="blocked by robots.txt")
                        continue
                    visited.add(url)
                    allowed.append(url)

                results = await asyncio.gather(
                    *(self._fetch_page(client, url, base_origin, request) for url in allowed)
                )
                for requested_url, result in zip(allowed, results):
                    fetches[requested_url] = result
                    if result.url != requested_url and _same_origin(result.url, base_origin):
                        fetches[result.url] = result
                    if result.parsed is None:
                        continue
                    for raw_href in result.parsed.links:
                        if not raw_href or raw_href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                            continue
                        absolute = _canonicalize(urljoin(result.url, raw_href), request.include_query_strings)
                        if _same_origin(absolute, base_origin):
                            graph[result.url].add(absolute)
                            if absolute not in queued and absolute not in visited:
                                queued.add(absolute)
                                pending.append(absolute)
                        else:
                            external_counts[result.url] += 1

            if pending:
                warnings.append("crawl stopped at max_pages before the entire discovered site was fetched")

        incoming: dict[str, int] = defaultdict(int)
        for source, targets in graph.items():
            for target in targets:
                incoming[target] += 1

        pages: list[WebsitePage] = []
        successful_html: dict[str, _FetchResult] = {}
        failed_pages = 0
        for url in sorted(fetches):
            result = fetches[url]
            if result.error or result.status_code >= 400 or result.status_code == 0:
                failed_pages += 1
            if result.parsed is not None and result.status_code < 400:
                successful_html[url] = result
            pages.append(
                WebsitePage(
                    url=url,
                    status_code=result.status_code,
                    content_type=result.content_type,
                    title=(result.parsed.title or None) if result.parsed else None,
                    h1=(result.parsed.h1 or None) if result.parsed else None,
                    word_count=len(result.parsed.text.split()) if result.parsed else 0,
                    internal_links=len(graph.get(url, set())),
                    external_links=external_counts.get(url, 0),
                    incoming_internal_links=incoming.get(url, 0),
                    truncated=result.truncated,
                )
            )

        broken: list[BrokenInternalLink] = []
        broken_targets = {
            url: result
            for url, result in fetches.items()
            if result.error or result.status_code >= 400 or result.status_code == 0
        }
        for source, targets in graph.items():
            for target in sorted(targets):
                result = broken_targets.get(target)
                if result is None:
                    continue
                broken.append(
                    BrokenInternalLink(
                        source_url=source,
                        target_url=target,
                        status_code=result.status_code or None,
                        error=result.error,
                    )
                )

        orphan_pages = sorted(
            url for url in successful_html if url != start and incoming.get(url, 0) == 0
        )

        suggestions: list[InternalLinkSuggestion] = []
        logical_pairs = 0
        candidate_pairs = 0
        if len(successful_html) >= 2:
            semantic_items: list[SemanticItem] = []
            for url, result in successful_html.items():
                parsed = result.parsed
                assert parsed is not None
                semantic_text = " ".join(
                    part for part in [parsed.title, parsed.h1, parsed.text[:4000]] if part
                )
                semantic_items.append(
                    SemanticItem(
                        id=url,
                        text=semantic_text or url,
                        metadata={"title": parsed.title, "h1": parsed.h1},
                    )
                )
            matrix = self.matrix.run(
                SemanticMatrixRequest(
                    left=semantic_items,
                    right=semantic_items,
                    top_k_per_left=min(
                        max(request.suggestions_per_page * 6, 20), len(semantic_items) - 1
                    ),
                    min_score=request.min_semantic_score,
                    exclude_same_id=True,
                    max_logical_pairs=max(2_000_000, len(semantic_items) ** 2),
                )
            )
            logical_pairs = matrix.logical_pairs
            candidate_pairs = matrix.candidate_pairs_scored
            by_source: dict[str, int] = defaultdict(int)
            for match in matrix.matches:
                if by_source[match.left_id] >= request.suggestions_per_page:
                    continue
                if match.right_id in graph.get(match.left_id, set()):
                    continue
                source = successful_html.get(match.left_id)
                target = successful_html.get(match.right_id)
                if source is None or target is None or source.parsed is None or target.parsed is None:
                    continue
                anchor = (target.parsed.title or target.parsed.h1 or match.right_id)[:160]
                source_text = source.parsed.text.casefold()
                anchor_present = bool(anchor and anchor.casefold() in source_text)
                reason_codes = ["LOCAL_SEMANTIC_RELEVANCE"]
                if anchor_present:
                    reason_codes.append("TARGET_ANCHOR_TEXT_PRESENT")
                suggestions.append(
                    InternalLinkSuggestion(
                        source_url=match.left_id,
                        target_url=match.right_id,
                        score=match.score,
                        suggested_anchor=anchor,
                        anchor_text_present=anchor_present,
                        reason_codes=reason_codes,
                    )
                )
                by_source[match.left_id] += 1

        return WebsiteAuditResponse(
            start_url=start,
            pages_discovered=len(queued),
            pages_crawled=len(fetches),
            html_pages=len(successful_html),
            failed_pages=failed_pages,
            internal_links_observed=sum(len(targets) for targets in graph.values()),
            external_links_observed=sum(external_counts.values()),
            orphan_pages=orphan_pages,
            broken_internal_links=broken,
            internal_link_suggestions=suggestions,
            pages=pages,
            logical_semantic_pairs=logical_pairs,
            semantic_candidate_pairs_scored=candidate_pairs,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            warnings=warnings,
        )


__all__ = [
    "WebsiteAuditEngine",
    "WebsiteAuditRequest",
    "WebsiteAuditResponse",
    "WebsitePage",
    "BrokenInternalLink",
    "InternalLinkSuggestion",
    "_HTMLCollector",
    "_assert_public_url",
]
