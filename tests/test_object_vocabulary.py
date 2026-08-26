"""Tests for prompt-to-object matching helpers."""

from src.perception.object_vocabulary import match_query_to_objects, best_label_match


def test_best_label_match_maps_synonyms():
    label, score, alias = best_label_match("mug")
    assert label == "cup"
    assert score > 0.6
    assert alias


def test_match_query_to_objects_prefers_best_label():
    result = match_query_to_objects(
        "mug",
        [
            {"label": "cup", "confidence": 0.92, "bbox": [1, 2, 3, 4]},
            {"label": "book", "confidence": 0.8, "bbox": [5, 6, 7, 8]},
        ],
    )
    assert result["ok"] is True
    assert result["results"][0]["label"] == "cup"


def test_match_query_to_objects_handles_no_match():
    result = match_query_to_objects(
        "spaceship",
        [{"label": "cup", "confidence": 0.92, "bbox": [1, 2, 3, 4]}],
    )
    assert result["ok"] is False
