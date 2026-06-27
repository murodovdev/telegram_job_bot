"""Tests for the SQLite persistence layer (bot.database)."""


def test_block_unblock_roundtrip(fresh_db):
    db = fresh_db
    assert db.is_blocked(123) is False
    assert db.block_user(123, "Spammer") is True
    assert db.block_user(123, "Spammer") is False        # already blocked
    assert db.is_blocked(123) is True
    assert db.is_blocked_cached(123) is True             # cache reflects write
    assert db.unblock_user(123) is True
    assert db.is_blocked(123) is False


def test_get_source_group_by_id_handles_legacy_bare_id(fresh_db):
    """A row stored WITHOUT the -100 prefix (bare channel id) must still be
    found when queried with the -100 supergroup form.

    Regression for the lstrip('100') bug: -1001000748619 must resolve to the
    bare id 1000748619, not the mangled 748619.
    """
    db = fresh_db
    db.add_source_group(chat_id=1000748619, title="Legacy Group", username=None)
    found = db.get_source_group_by_id(-1001000748619)
    assert found is not None
    assert found.chat_id == 1000748619


def test_content_dedup_exact_and_fuzzy(fresh_db):
    db = fresh_db
    text = "Seoul da ishchi kerak. Oylik 2.5 million. Smena 12 soat."

    # Not seen yet
    assert db.is_content_duplicate(text)[0] is False

    db.record_content_hash(text, source_chat=-100, source_msg=1)

    # Exact match (Tier 1)
    assert db.is_content_duplicate(text)[0] is True

    # Near-duplicate with a small appended line (Tier 2 fuzzy)
    near = text + "\nTel: +82 10 1234 5678"
    assert db.is_content_duplicate(near, similarity_threshold=0.85)[0] is True

    # Completely different text must not be flagged
    different = "Busan shahrida tajribali dizayner qidiryapmiz portfolio bilan"
    assert db.is_content_duplicate(different)[0] is False


def test_content_dedup_disabled_window(fresh_db):
    db = fresh_db
    assert db.is_content_duplicate("anything", window_hours=0) == (False, "")


def test_settings_roundtrip_and_cache(fresh_db):
    db = fresh_db
    # AI filter defaults to enabled
    assert db.is_ai_filter_enabled() is True
    new_state = db.toggle_ai_filter()
    assert new_state is False
    assert db.is_ai_filter_enabled() is False
