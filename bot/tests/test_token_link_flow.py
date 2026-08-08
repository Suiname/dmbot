from bot.config import settings
from bot.services.token_link_flow import LEADERBOARD_URL, MSG_LINK_OFF_BOARD


def test_leaderboard_url_is_built_from_the_configured_public_site():
    """Phase 4 replaced a hardcoded LLU leaderboard URL with settings.leaderboard_url (derived from
    settings.public_site_url) — a regression here points every 17lands-linked player at LLU's site."""
    assert LEADERBOARD_URL == settings.leaderboard_url
    assert LEADERBOARD_URL in MSG_LINK_OFF_BOARD
    assert "limitedlevelups" not in MSG_LINK_OFF_BOARD.lower()
