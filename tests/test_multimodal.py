import pytest

from app.multimodal import average_scores, detect_media_kind


def test_detect_media_kind_by_mime_and_suffix():
    assert detect_media_kind("x.jpg", "image/jpeg") == "image"
    assert detect_media_kind("x.pdf", "application/octet-stream") == "pdf"
    assert detect_media_kind("x.wav", "audio/wav") == "audio"


def test_detect_media_kind_rejects_unknown():
    with pytest.raises(ValueError):
        detect_media_kind("x.exe", "application/octet-stream")


def test_average_scores_normalizes_multiple_images():
    scores = average_scores(
        [
            {"clean": 0.8, "damaged": 0.2},
            {"clean": 0.6, "damaged": 0.4},
        ],
        ["clean", "damaged"],
    )
    assert round(scores["clean"], 3) == 0.7
    assert round(scores["damaged"], 3) == 0.3
    assert round(sum(scores.values()), 6) == 1.0
