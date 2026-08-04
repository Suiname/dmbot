from bot.config import settings
from bot.services.media_sync import _clean_title


def test_clean_title_strips_the_configured_podcast_prefix(monkeypatch):
    monkeypatch.setattr(settings, "podcast_title_prefix", "MyShow")

    assert _clean_title("MyShow #12: A Great Episode") == "A Great Episode"


def test_clean_title_passes_titles_through_unchanged_without_a_configured_prefix(monkeypatch):
    """A fresh deployment that hasn't set PODCAST_TITLE_PREFIX must not strip anything — the config
    default is None, unlike upstream's hardcoded LLU show name."""
    monkeypatch.setattr(settings, "podcast_title_prefix", None)

    assert _clean_title("MyShow #12: A Great Episode") == "MyShow #12: A Great Episode"
