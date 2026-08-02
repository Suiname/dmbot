from datetime import datetime, timezone

import pytest

from bot import emojis
from bot.config import settings
from bot.services.format_schedule import (
    FORMAT_ARCHIVE_CATEGORY,
    LATEST_SET_CATEGORY,
    archive_candidates,
    channel_matches_set,
)
from bot.services.server_guide import (
    GUIDE_PAGES,
    find_channel,
    pages_by_channel,
    parse_page,
    render_page,
    stripped_channel_name,
)

MSH_ACTIVE = datetime(2026, 7, 1, tzinfo=timezone.utc)
SOS_ACTIVE = datetime(2026, 5, 15, tzinfo=timezone.utc)


class _StubCategory:
    def __init__(self, name):
        self.name = name


class _StubChannel:
    def __init__(self, name, category=None, created_at=None):
        self.name = name
        self.category = _StubCategory(category) if category else None
        self.created_at = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.mention = f"<#{name}>"


@pytest.mark.parametrize("raw, expected", [
    ("🧭-channel-overview", "channel-overview"),
    ("📏-rules", "rules"),
    ("🌊🌿🔥🌪️-avatar", "avatar"),
    ("plain-name", "plain-name"),
])
def test_stripped_channel_name(raw, expected):
    assert stripped_channel_name(raw) == expected


def test_find_channel_prefers_exact_stripped_name_over_substring():
    channels = [
        _StubChannel("⚖-mtg-rules-questions"),
        _StubChannel("📏-rules"),
    ]

    found = find_channel(channels, "rules")

    assert found.name == "📏-rules"


def test_find_channel_falls_back_to_substring_then_none():
    channels = [_StubChannel("🚀-pod-draft-coordination")]

    assert find_channel(channels, "pod-draft").name == "🚀-pod-draft-coordination"
    assert find_channel(channels, "quick-links") is None


@pytest.mark.parametrize("channel_name, set_name, expected", [
    ("🦸-marvel-super-heroes", "Marvel Super Heroes", True),
    ("🦉🏫-strixhaven", "Strixhaven: School of Mages", True),
    ("🤫-secrets-of-strixhaven", "Secrets of Strixhaven", True),
    ("🤫-secrets-of-strixhaven", "Strixhaven: School of Mages", False),
    ("📦-cube-talk", "MTGA Cube", False),
    ("❓-whats-the-pick", "Marvel Super Heroes", False),
    ("🌅-modern-horizons-2", "Modern Horizons 3", False),
])
def test_channel_matches_set(channel_name, set_name, expected):
    assert channel_matches_set(channel_name, set_name) is expected


def test_archive_candidates_only_stale_set_channels():
    channels = [
        _StubChannel("🦸-marvel-super-heroes", LATEST_SET_CATEGORY),
        _StubChannel("🤫-secrets-of-strixhaven", LATEST_SET_CATEGORY),
        _StubChannel("❓-whats-the-pick", LATEST_SET_CATEGORY),
        _StubChannel("📦-cube-talk", LATEST_SET_CATEGORY),
        _StubChannel("🐥-final-fantasy", FORMAT_ARCHIVE_CATEGORY),
        _StubChannel("😁-magic-and-chill", "MTG General"),
    ]

    stale = archive_candidates(channels, MSH_ACTIVE)

    assert [channel.name for channel in stale] == ["🤫-secrets-of-strixhaven"]


def test_archive_candidates_keeps_upcoming_set_channel_during_coexistence():
    channels = [
        _StubChannel("🤫-secrets-of-strixhaven", LATEST_SET_CATEGORY),
        _StubChannel("🦸-marvel-super-heroes", LATEST_SET_CATEGORY),
        _StubChannel("🐢-teenage-mutant-ninja-turtles", LATEST_SET_CATEGORY),
    ]

    stale = archive_candidates(channels, SOS_ACTIVE)

    assert [channel.name for channel in stale] == ["🐢-teenage-mutant-ninja-turtles"]


def _full_channel_set():
    strategy = [
        "❓-whats-the-build", "❓-whats-the-pick", "❓-whats-the-play",
    ]
    other = [
        "😁-magic-and-chill", "🤫-preview-season", "🖼-preview-season-images",
        "🚀-pod-draft-coordination", "📱📺-recent-episode-and-video-discussion",
        "👀-draft-log-review", "🧎-high-stakes-deck-help", "🤑-sick-brags",
        "📺-submissions-for-video-draft-log-reviews", "📺-submissions-for-gameplay-video-review",
        "🐱🐶-pet-pics", "🧭-channel-overview", "🔗-quick-links", "📏-rules", "🌐-limitedlevelups-com",
    ]
    newest_set_channel = _StubChannel("🦸-marvel-super-heroes", LATEST_SET_CATEGORY,
                                      created_at=datetime(2026, 6, 8, tzinfo=timezone.utc))
    return ([newest_set_channel]
            + [_StubChannel(name, LATEST_SET_CATEGORY) for name in strategy]
            + [_StubChannel(name) for name in other])


@pytest.mark.parametrize("page", GUIDE_PAGES, ids=lambda page: page.name)
def test_every_page_renders_with_no_unresolved_placeholders(page):
    channels = _full_channel_set()

    content = render_page(page.name, channels, bot_mention="<@42>")

    assert content.title and not content.title.startswith("#")
    assert "{" not in content.body
    assert "}" not in content.body
    assert content.topic is None or "{" not in content.topic
    assert content.thumbnail is None or "{" not in content.thumbnail


def test_render_page_resolves_channel_mentions_and_latest_set():
    channels = _full_channel_set()

    content = render_page("channel-overview", channels)

    assert "<#🚀-pod-draft-coordination>" in content.body


def test_render_page_missing_channel_degrades_to_plain_name():
    channels = [_StubChannel("🦸-marvel-super-heroes", LATEST_SET_CATEGORY)]

    content = render_page("channel-overview", channels)

    assert "#pod-draft-chat" in content.body


def test_render_page_inlines_site_url(monkeypatch):
    monkeypatch.setattr(settings, "public_site_url", "https://example.com")
    content = render_page("channel-overview", _full_channel_set())

    assert content.topic


def test_render_page_inlines_bot_mention():
    content = render_page("dischord-bot", _full_channel_set(), bot_mention="<@42>")

    assert "<@42>" in content.body


def test_render_page_resolves_thumbnail_and_feedback():
    content = render_page("dischord-bot", _full_channel_set())

    assert f"<#{settings.feedback_channel_id}>" in content.body


def test_render_page_resolves_command_descriptions():
    from bot.commands import descriptions

    content = render_page("dischord-bot", _full_channel_set())

    assert descriptions.POD_GUIDE in content.body
    assert descriptions.HELP in content.body
    assert "{desc:" not in content.body


def test_render_page_resolves_loaded_emojis(monkeypatch):
    class _StubEmoji:
        def __str__(self):
            return "<:llu:123>"

    monkeypatch.setattr(emojis, "_EMOJIS", {"llu": _StubEmoji()})

    content = render_page("dischord-bot", _full_channel_set(), bot_mention="<@42>")

    assert "<:llu:123>" in content.body


def test_pages_by_channel_has_no_duplicate_channels_and_bot_guide_maps_to_dischord_bot():
    groups = pages_by_channel()

    channels = [channel for channel, _ in groups]
    bot_guide = next(pages for channel, pages in groups if channel == "bot-guide")

    assert len(channels) == len(set(channels))
    assert [page.name for page in bot_guide] == ["dischord-bot"]


def test_render_page_resolves_moderator_mention():
    content = render_page("rules", _full_channel_set(), mod_mention="<@&99>")

    assert "<@&99>" in content.body
    assert "{moderator}" not in content.body


def test_render_page_resolves_pod_drafters_mention():
    content = render_page("dischord-bot", _full_channel_set(), pod_drafters_mention="<@&77>")

    assert "<@&77>" in content.body
    assert "{pod-drafters}" not in content.body


def test_parse_page_splits_topic_from_body():
    content = parse_page("rules")

    assert content.title
    assert content.topic
    assert "Topic:" not in content.body


@pytest.mark.parametrize("page", GUIDE_PAGES, ids=lambda page: page.name)
def test_page_bodies_fit_one_embed(page):
    content = parse_page(page.name)

    assert len(content.body) < 4000
