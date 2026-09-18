import math
from app.ml.local_classifier import hashed_char_features, normalize_text


def test_japanese_normalization_and_features():
    assert normalize_text(" Ｗｉ－Ｆｉが使えない ") == "wi-fiが使えない"
    vector = hashed_char_features("エアコンが動きません", feature_dim=512, ngram_min=1, ngram_max=3)
    assert len(vector) == 512
    norm = math.sqrt(sum(value * value for value in vector))
    assert abs(norm - 1.0) < 1e-6
    assert any(value != 0 for value in vector)


def test_hash_is_deterministic_and_language_agnostic():
    first = hashed_char_features("チェックインは何時ですか", feature_dim=256)
    second = hashed_char_features("チェックインは何時ですか", feature_dim=256)
    english = hashed_char_features("What time is check-in?", feature_dim=256)
    assert first == second
    assert first != english
