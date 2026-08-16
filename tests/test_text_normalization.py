import pytest

from app.text.normalization import normalize_extracted_text, normalize_for_search


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("efﬁciency", "efficiency"),
        ("ﬂow", "flow"),
        ("L¨u", "Lü"),
        ("a\u00adb", "ab"),
        ("a\u00a0b", "a b"),
        ("中文–数学 α", "中文–数学 α"),
    ],
)
def test_normalization_golden(raw: str, expected: str) -> None:
    assert normalize_extracted_text(raw)[0] == expected


def test_normalization_is_idempotent() -> None:
    once, _ = normalize_extracted_text("efﬁciency L¨u")
    twice, stats = normalize_extracted_text(once)
    assert twice == once
    assert stats.ligatures_expanded == 0
    assert normalize_for_search("ﬁnance") == "finance"
