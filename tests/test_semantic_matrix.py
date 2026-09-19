import pytest

from app.semantic_matrix import SemanticItem, SemanticMatrixEngine, SemanticMatrixRequest
from app.web_intelligence import _HTMLCollector, _assert_public_url


def test_semantic_matrix_ranks_related_japanese_text_first():
    engine = SemanticMatrixEngine()
    result = engine.run(
        SemanticMatrixRequest(
            left=[SemanticItem(id="q", text="エアコンが冷えない 故障 修理")],
            right=[
                SemanticItem(id="aircon", text="エアコン 冷房が効かない 故障時の確認方法"),
                SemanticItem(id="payment", text="クレジットカード 支払い 請求書"),
                SemanticItem(id="wifi", text="Wi-Fi 接続 パスワード インターネット"),
            ],
            top_k_per_left=2,
        )
    )
    assert result.logical_pairs == 3
    assert result.matches
    assert result.matches[0].right_id == "aircon"
    assert result.matches[0].score > 0
    assert result.candidate_pairs_scored <= result.logical_pairs


def test_semantic_matrix_rejects_unbounded_pair_explosion():
    left = [SemanticItem(id=f"l{i}", text="same") for i in range(3)]
    right = [SemanticItem(id=f"r{i}", text="same") for i in range(3)]
    with pytest.raises(ValueError):
        SemanticMatrixRequest(left=left, right=right, max_logical_pairs=8)


def test_html_collector_extracts_title_h1_text_and_links():
    parser = _HTMLCollector()
    parser.feed(
        """
        <html><head><title>Sample Page</title><script>ignore me</script></head>
        <body><h1>Main Heading</h1><p>Useful body text</p>
        <a href="/guide">Guide</a><a href="https://outside.example/x">Outside</a></body></html>
        """
    )
    parsed = parser.parsed()
    assert parsed.title == "Sample Page"
    assert parsed.h1 == "Main Heading"
    assert "Useful body text" in parsed.text
    assert "ignore me" not in parsed.text
    assert parsed.links == ["/guide", "https://outside.example/x"]


@pytest.mark.asyncio
async def test_website_fetch_guard_rejects_private_addresses():
    with pytest.raises(ValueError):
        await _assert_public_url("http://127.0.0.1/")
    with pytest.raises(ValueError):
        await _assert_public_url("http://10.0.0.5/")
    with pytest.raises(ValueError):
        await _assert_public_url("http://169.254.169.254/latest/meta-data/")
