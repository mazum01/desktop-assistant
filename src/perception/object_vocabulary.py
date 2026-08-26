"""Prompt-to-object matching helpers for COCO-style detectors.

These helpers do not turn a closed-set detector into a true open-vocabulary
model. They do, however, make the existing detector much more usable by
normalizing synonyms, plurals, and fuzzy user phrasing onto the best available
detected object.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

from src.perception.object_detector import COCO_CLASSES

_WORD_RE = re.compile(r"[a-z0-9]+")
_SEP_RE = re.compile(r"\s*(?:,|/|&|\+|\band\b|\bor\b)\s*")

_ALIASES: dict[str, list[str]] = {
    "person": ["human", "man", "woman", "child", "people", "guy", "girl"],
    "bicycle": ["bike", "cycle", "push bike"],
    "car": ["auto", "automobile", "vehicle", "sedan"],
    "motorcycle": ["motor bike", "motorbike", "scooter"],
    "bus": ["coach", "shuttle"],
    "truck": ["lorry", "pickup", "pickup truck"],
    "traffic light": ["stop light", "signal light"],
    "bench": ["seat", "park bench"],
    "cat": ["kitty", "kitten"],
    "dog": ["puppy", "hound"],
    "backpack": ["pack", "rucksack"],
    "umbrella": ["parasol"],
    "handbag": ["purse", "bag", "hand bag"],
    "suitcase": ["luggage", "carry-on", "carryon"],
    "sports ball": ["ball", "soccer ball", "football", "basketball"],
    "bottle": ["water bottle", "drink bottle", "flask"],
    "cup": ["mug", "coffee cup", "tea cup", "glass", "drinking cup"],
    "fork": ["utensil", "spork"],
    "knife": ["blade", "cutlery knife"],
    "spoon": ["utensil", "cutlery spoon"],
    "banana": ["plantain"],
    "apple": ["fruit"],
    "sandwich": ["sub", "sub sandwich", "sandwiches"],
    "chair": ["seat", "stool"],
    "couch": ["sofa", "settee"],
    "potted plant": ["plant", "houseplant", "house plant"],
    "bed": ["mattress", "bunk", "cot"],
    "dining table": ["table", "desk", "work table"],
    "toilet": ["lavatory", "loo", "wc"],
    "tv": ["television", "monitor", "screen", "display", "panel"],
    "laptop": ["computer", "notebook", "macbook", "portable computer"],
    "mouse": ["computer mouse", "pointer"],
    "remote": ["remote control", "controller", "clicker"],
    "keyboard": ["keypad", "typing keyboard"],
    "cell phone": ["phone", "mobile", "mobile phone", "cellphone", "smartphone"],
    "microwave": ["microwave oven", "mw oven"],
    "oven": ["stove", "range"],
    "toaster": ["toast maker"],
    "sink": ["basin", "wash basin"],
    "refrigerator": ["fridge", "icebox"],
    "book": ["novel", "magazine", "textbook"],
    "clock": ["watch", "timepiece"],
    "vase": ["flower vase", "urn"],
    "scissors": ["shears"],
    "teddy bear": ["bear", "stuffed bear", "plush bear"],
    "hair drier": ["hair dryer", "blower"],
    "toothbrush": ["brush"],
}


def normalize_phrase(text: str) -> str:
    """Normalize a free-form query to a stable comparison string."""
    words = _WORD_RE.findall(text.lower().replace("-", " "))
    return " ".join(words)


def split_query_terms(query: str) -> list[str]:
    """Split a natural-language object query into search terms."""
    parts = [p.strip() for p in _SEP_RE.split(query) if p and p.strip()]
    if len(parts) <= 1:
        return [query.strip()] if query.strip() else []
    return parts


def _simple_variants(label: str) -> list[str]:
    variants = [label]
    if " " in label:
        variants.append(label.replace(" ", ""))
        variants.append(label.replace(" ", "-"))
    if label.endswith("ies"):
        variants.append(label[:-3] + "y")
    elif label.endswith("s") and len(label) > 3:
        variants.append(label[:-1])
    else:
        variants.append(label + "s")
    return list(dict.fromkeys(variants))


def aliases_for_label(label: str) -> list[str]:
    """Return normalized alias candidates for a canonical COCO label."""
    aliases = [label, *(_ALIASES.get(label, [])), *_simple_variants(label)]
    return list(dict.fromkeys(normalize_phrase(alias) for alias in aliases if alias))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_words = set(a.split())
    b_words = set(b.split())
    overlap = len(a_words & b_words) / max(1, len(a_words | b_words))
    ratio = SequenceMatcher(None, a, b).ratio()
    if a_words <= b_words or b_words <= a_words:
        overlap = max(overlap, 0.95)
    return (0.65 * ratio) + (0.35 * overlap)


def best_label_match(term: str) -> tuple[str | None, float, str]:
    """Return the best canonical label for *term*.

    Returns ``(label, score, matched_alias)``. The score is in the range 0–1.
    """
    normalized = normalize_phrase(term)
    if not normalized:
        return None, 0.0, ""

    best_label: str | None = None
    best_alias = ""
    best_score = 0.0
    for label in COCO_CLASSES:
        for alias in aliases_for_label(label):
            score = _similarity(normalized, alias)
            if score > best_score:
                best_label = label
                best_alias = alias
                best_score = score
    return best_label, best_score, best_alias


def score_detection_against_term(label: str, term: str) -> tuple[float, str, str]:
    """Score a detected label against a free-form term."""
    canonical_term, term_score, matched_alias = best_label_match(term)
    if canonical_term is None:
        return 0.0, "", ""

    det_aliases = aliases_for_label(label)
    if canonical_term == label:
        return max(term_score, 0.95), canonical_term, matched_alias or label

    best_det_score = 0.0
    for alias in det_aliases:
        best_det_score = max(best_det_score, _similarity(normalize_phrase(canonical_term), alias))

    return best_det_score * max(0.5, term_score), canonical_term, matched_alias


def _object_text(obj: dict) -> str:
    label = str(obj.get("label", "")).strip()
    if not label:
        return ""
    parts = [label]
    for alias in aliases_for_label(label):
        if alias and alias != normalize_phrase(label):
            parts.append(alias)
    return " ".join(dict.fromkeys(parts))


def match_query_to_objects(query: str, objects: Iterable[dict], *, threshold: float = 0.55) -> dict:
    """Match a free-form query against a list of detected objects."""
    terms = split_query_terms(query)
    detections = [obj for obj in objects if isinstance(obj, dict)]
    results: list[dict] = []

    if not terms:
        return {
            "ok": False,
            "query": query,
            "terms": [],
            "results": [],
            "message": "Please name the object you want me to look for.",
        }

    for term in terms:
        term_norm = normalize_phrase(term)
        if not term_norm:
            continue
        best: dict | None = None
        best_score = 0.0
        for obj in detections:
            label = str(obj.get("label", "")).strip()
            if not label:
                continue
            label_score, canonical_term, matched_alias = score_detection_against_term(label, term)
            conf = float(obj.get("confidence", 0.0) or 0.0)
            combined = label_score * (0.45 + 0.55 * max(0.0, min(conf, 1.0)))
            if combined > best_score:
                best_score = combined
                best = {
                    "term": term,
                    "term_normalized": term_norm,
                    "requested_label": canonical_term,
                    "matched_alias": matched_alias,
                    "label": label,
                    "class_id": obj.get("class_id"),
                    "confidence": conf,
                    "bbox": obj.get("bbox"),
                    "score": round(best_score, 3),
                }
        if best and best_score >= threshold:
            results.append(best)

    if not results:
        return {
            "ok": False,
            "query": query,
            "terms": terms,
            "results": [],
            "message": f"I don't see a good match for {query!r}.",
        }

    if len(results) == 1:
        item = results[0]
        phrase = item["label"]
        if item["requested_label"] and item["requested_label"] != item["label"]:
            phrase = f"{item['label']} (closest to {item['requested_label']})"
        return {
            "ok": True,
            "query": query,
            "terms": terms,
            "results": results,
            "message": f"I see {phrase} for {item['term']!r}.",
        }

    spoken = []
    for item in results:
        phrase = item["label"]
        if item["requested_label"] and item["requested_label"] != item["label"]:
            phrase = f"{item['label']} for {item['term']!r}"
        spoken.append(phrase)
    return {
        "ok": True,
        "query": query,
        "terms": terms,
        "results": results,
        "message": "I see " + ", ".join(spoken) + ".",
    }
