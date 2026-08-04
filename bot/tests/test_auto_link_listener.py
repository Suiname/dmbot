from bot.config import settings
from bot.listeners.auto_link_listener import updated_message


def test_updated_message_links_the_configured_leaderboard(monkeypatch):
    monkeypatch.setattr(settings, "public_site_url", "https://example.com")

    assert "https://example.com/leaderboard" in updated_message(opted_in=True)
    assert "https://example.com/leaderboard" in updated_message(opted_in=False)
