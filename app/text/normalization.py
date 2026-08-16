"""Conservative, idempotent normalization for extraction and retrieval."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


_LIGATURES = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "st",
        "\ufb06": "st",
    }
)
_SPACING_DIAERESIS = re.compile(r"([A-Za-z])\s*¨\s*([A-Za-z])")


@dataclass(frozen=True, slots=True)
class TextStats:
    ligatures_expanded: int = 0
    soft_hyphens_removed: int = 0
    nonbreaking_spaces_replaced: int = 0
    spacing_accents_repaired: int = 0


def normalize_extracted_text(text: str) -> tuple[str, TextStats]:
    """Normalize high-confidence extraction artifacts without compatibility folding."""

    ligatures = sum(text.count(chr(codepoint)) for codepoint in _LIGATURES)
    soft_hyphens = text.count("\u00ad")
    spaces = text.count("\u00a0")
    accents = len(_SPACING_DIAERESIS.findall(text))
    value = text.translate(_LIGATURES).replace("\u00ad", "").replace("\u00a0", " ")
    value = _SPACING_DIAERESIS.sub(lambda match: match.group(1) + match.group(2) + "\u0308", value)
    value = unicodedata.normalize("NFC", value)
    return value, TextStats(ligatures, soft_hyphens, spaces, accents)


def normalize_for_search(text: str | None) -> str:
    """Use the same normalization for indexed text and incoming queries."""

    if not text:
        return ""
    return normalize_extracted_text(text)[0]
