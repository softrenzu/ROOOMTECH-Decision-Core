import pytest

import app.web_intelligence as web
from app.web_intelligence import WebsiteAuditEngine, WebsiteAuditRequest, _FetchResult, _ParsedHTML


class FakeWebsiteAuditEngine(WebsiteAuditEngine):
    async def _fetch_page(self, client, url, base_origin, request):
        fixtures = {
            "https://example.com/": _FetchResult(
                "https://example.com/",
                200,
                "text/html",
                _ParsedHTML(
                    "Home",
                    "Home",
                    "Product overview and installation resources",
                    ["/product", "/guide", "/missing"],
                ),
            ),
            "https://example.com/product": _FetchResult(
                "https://example.com/product",
                200,
                "text/html",
                _ParsedHTML(
                    "Product",
                    "Product",
                    "Our product installation guide explains setup and installation steps",
                    [],
                ),
            ),
            "https://example.com/guide": _FetchResult(
                "https://example.com/guide",
                200,
                "text/html",
                _ParsedHTML(
                    "Installation Guide",
                    "Installation Guide",
                    "Installation guide with setup steps for the product",
                    [],
                ),
            ),
            "https://example.com/missing": _FetchResult(
                "https://example.com/missing",
                404,
                "text/html",
                None,
            ),
        }
        return fixtures[url]


@pytest.mark.asyncio
async def test_website_audit_builds_graph_and_suggests_missing_link(monkeypatch):
    async def allow_test_host(url):
        return None

    monkeypatch.setattr(web, "_assert_public_url", allow_test_host)
    engine = FakeWebsiteAuditEngine()
    result = await engine.audit(
        WebsiteAuditRequest(
            start_url="https://example.com/",
            max_pages=10,
            concurrency=4,
            respect_robots_txt=False,
            suggestions_per_page=3,
            min_semantic_score=0.05,
        )
    )

    assert result.pages_crawled == 4
    assert result.html_pages == 3
    assert result.failed_pages == 1
    assert any(
        item.source_url == "https://example.com/"
        and item.target_url == "https://example.com/missing"
        for item in result.broken_internal_links
    )
    assert any(
        item.source_url == "https://example.com/product"
        and item.target_url == "https://example.com/guide"
        for item in result.internal_link_suggestions
    )
    assert result.logical_semantic_pairs == 9
    assert result.persistence == "none"
