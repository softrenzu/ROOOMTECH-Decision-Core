from .adaptive_classifier import AdaptiveLocalClassifierProvider
from .local_classifier import hashed_char_features

LocalClassifierProvider = AdaptiveLocalClassifierProvider

__all__ = ["AdaptiveLocalClassifierProvider", "LocalClassifierProvider", "hashed_char_features"]
