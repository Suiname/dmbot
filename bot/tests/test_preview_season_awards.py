from bot.commands.preview_season_awards import msg_counted
from bot.config import settings


def test_msg_counted_links_the_configured_preview_channel(monkeypatch):
    monkeypatch.setattr(settings, "production_guild_id", 111)
    monkeypatch.setattr(settings, "preview_season_channel_id", 222)

    text = msg_counted(5, "https://example.com/ceremony", "👆")

    assert "https://discord.com/channels/111/222" in text
    assert "**5**" in text


def test_msg_counted_falls_back_to_plain_text_without_a_configured_channel(monkeypatch):
    """A fresh deployment that hasn't set PREVIEW_SEASON_CHANNEL_ID must not crash or link a bogus URL
    built from `None` — Phase 3 of the generalize plan added this fallback."""
    monkeypatch.setattr(settings, "production_guild_id", None)
    monkeypatch.setattr(settings, "preview_season_channel_id", None)

    text = msg_counted(5, "https://example.com/ceremony", "👆")

    assert "discord.com/channels" not in text
    assert "preview-season" in text
