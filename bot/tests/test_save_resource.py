from datetime import datetime, timezone

import pytest

from bot.commands.save_resource import (
    THREAD_NAME_LIMIT,
    WEBHOOK_NAME,
    default_thread_name,
    editable_body_from_starter,
    merge_texts,
    split_content,
)
from bot.config import settings


def _at(minute: int) -> datetime:
    return datetime(2026, 7, 1, 12, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("content,expected", [
    ("Great limited primer", "Great limited primer"),
    ("\n\n  First real line  \nsecond line", "First real line"),
    ("Tabs\tand   runs  of spaces", "Tabs and runs of spaces"),
    ("x" * 150, "x" * THREAD_NAME_LIMIT),
    ("", "Resource from Chandra"),
    (None, "Resource from Chandra"),
    ("   \n \n", "Resource from Chandra"),
])
def test_default_thread_name(content, expected):
    assert default_thread_name(content, "Chandra") == expected


def test_default_thread_name_truncates_long_author_fallback():
    name = default_thread_name(None, "J" * 200)

    assert len(name) == THREAD_NAME_LIMIT


def test_split_content_short_text_is_single_chunk():
    assert split_content("hello", limit=100) == ["hello"]


def test_split_content_prefers_newline_boundaries():
    text = "a" * 60 + "\n" + "b" * 60

    chunks = split_content(text, limit=100)

    assert chunks == ["a" * 60, "b" * 60]


def test_split_content_hard_cuts_without_newlines():
    text = "a" * 250

    chunks = split_content(text, limit=100)

    assert chunks == ["a" * 100, "a" * 100, "a" * 50]
    assert "".join(chunks) == text


def test_split_content_never_exceeds_limit():
    text = ("word " * 30 + "\n") * 40

    chunks = split_content(text, limit=200)

    assert all(len(chunk) <= 200 for chunk in chunks)


def test_merge_texts_orders_chronologically():
    entries = [(_at(5), "second"), (_at(0), "first"), (_at(9), "third")]

    assert merge_texts(entries) == ["first", "second", "third"]


@pytest.mark.parametrize("entries,expected", [
    ([(_at(0), "  kept  "), (_at(1), "   "), (_at(2), None), (_at(3), "also kept")], ["kept", "also kept"]),
    ([(_at(0), None)], []),
    ([], []),
])
def test_merge_texts_drops_blank_entries(entries, expected):
    assert merge_texts(entries) == expected


@pytest.mark.parametrize("starter,expected", [
    ("the body\n\n-# Original post https://discord.com/x by <@1>", "the body"),
    ("the body\n\n-# Original post https://discord.com/x by <@1>, saved by <@2>", "the body"),
    ("📚 Saved by <@2> from <@1>'s [post](https://discord.com/x)\n\nthe body", "the body"),
    ("line one\nline two\n\n-# Original post https://discord.com/x by <@1>", "line one\nline two"),
    ("-# Original post https://discord.com/x by <@1>", ""),
])
def test_editable_body_from_starter_strips_attribution(starter, expected):
    assert editable_body_from_starter(starter) == expected


def test_webhook_name_carries_the_configured_community_name():
    assert WEBHOOK_NAME == f"{settings.community_name} Resources"
    assert "LLU" not in WEBHOOK_NAME
