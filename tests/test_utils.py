"""Tests for shared helpers (bot.utils)."""

from datetime import datetime, timezone
from types import SimpleNamespace

from bot.utils import build_job_post, truncate, _strip_markdown, get_user_link


def test_build_job_post_escapes_html():
    post = build_job_post(
        group_title="G&Co",
        group_link="https://t.me/x",
        author_name="Author",
        author_link=None,
        message_time=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
        message_text="ish bor & <script>alert()</script>",
        matched_keywords=["ish bor"],
    )
    assert "&amp;" in post              # & escaped
    assert "<script>" not in post       # raw tag must not survive
    assert "&lt;script&gt;" in post     # escaped form present


def test_build_job_post_renders_links():
    post = build_job_post(
        group_title="MyGroup",
        group_link="https://t.me/mygroup",
        author_name="Bob",
        author_link="https://t.me/bob",
        message_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        message_text="vakansiya",
        matched_keywords=[],
    )
    assert '<a href="https://t.me/mygroup">MyGroup</a>' in post
    assert '<a href="https://t.me/bob">Bob</a>' in post


def test_truncate():
    assert truncate("abc", 10) == "abc"
    assert truncate("a" * 20, 5) == "aaaaa…"


def test_strip_markdown():
    assert _strip_markdown("**bold**") == "bold"
    assert _strip_markdown("__under__") == "under"
    assert _strip_markdown("`code`") == "code"


def test_get_user_link():
    assert get_user_link(SimpleNamespace(username="bob", id=5)) == "https://t.me/bob"
    assert get_user_link(SimpleNamespace(username=None, id=5)) == "tg://user?id=5"
