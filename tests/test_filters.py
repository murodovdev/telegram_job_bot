"""Tests for the job-keyword detection engine (bot.filters)."""

import pytest

from bot.filters import is_job_message, _normalise


@pytest.mark.parametrize("text, lang", [
    ("Bizga ishchi kerak, oylik yaxshi", "uzbek"),
    ("Срочно нужен работник на склад", "russian"),
    ("서울 알바 구합니다 시급 12000", "korean"),
    ("vakansiya: ofis xodimi", "uzbek"),
])
def test_detects_job_posts(text, lang):
    result = is_job_message(text)
    assert result.is_job is True
    assert result.matched_lang == lang
    assert result.matched_keywords


@pytest.mark.parametrize("text", [
    "Salom, qalaysiz?",
    "Привет, как дела сегодня",
    "오늘 날씨가 정말 좋네요",
    "",
    "   ",
])
def test_ignores_non_job_text(text):
    assert is_job_message(text).is_job is False


def test_russian_muzhchina_keyword_matches():
    """Regression for the missing-comma bug that merged 'мужчина' into
    'мужчинамужчина нужен' and broke the standalone keyword."""
    result = is_job_message("Срочно мужчина на стройку")
    assert result.is_job is True
    assert result.matched_lang == "russian"


def test_separator_and_noise_normalisation():
    # hyphen/underscore/emoji glued to the keyword must still match
    assert is_job_message("ish-bor 💪").is_job is True
    assert is_job_message("(vakansiya!!!)").is_job is True


def test_normalise_collapses_separators_and_noise():
    assert _normalise("ISH_BOR") == "ish bor"
    assert _normalise("arbayt💪") == "arbayt"
    assert _normalise("  ish   bor  ") == "ish bor"
