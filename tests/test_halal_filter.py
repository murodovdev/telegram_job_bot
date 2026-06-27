"""Tests for the haram-detection engine (bot.halal_filter)."""

import pytest

from bot.halal_filter import is_haram_job


@pytest.mark.parametrize("text, category", [
    ("주류 판매 알바 모집", "alkogol"),
    ("카지노 딜러 구함", "qimor"),
    ("삼겹살 식당 직원", "cho'chqa"),
    ("보험영업 사원 모집", "riba"),
])
def test_detects_haram_categories(text, category):
    result = is_haram_job(text)
    assert result.is_haram is True
    assert result.category == category
    assert result.matched_keywords


@pytest.mark.parametrize("text", [
    "Dasturchi kerak, masofaviy ish",
    "Tarjimon kerak, ofis ishi",
    "데이터 입력 직원 구합니다",
    "",
])
def test_allows_clean_jobs(text):
    assert is_haram_job(text).is_haram is False


def test_gonjiam_cj_keyword_matches():
    """Regression for the missing-comma bug that merged 'GONJIAM CJ' into
    'GONJIAM CJGonjiam CJ' in the tekpe category."""
    result = is_haram_job("Bugun GONJIAM CJ ga ishchi kerak")
    assert result.is_haram is True
    assert result.category == "tekpe"
