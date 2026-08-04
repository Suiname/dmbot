from bot.config import settings
from bot.services.mtgscribe import REQUEST_HEADERS


def test_scribe_user_agent_carries_the_configured_community_and_site():
    """Phase 4 of the generalize plan replaced the hardcoded LLU site details in this header with
    settings.community_name / settings.public_site_url, so mtgscribe.com's allowlist entry keeps working
    on a fork instead of quietly identifying as LLU."""
    assert REQUEST_HEADERS["User-Agent"] == f"{settings.community_name}-Bot/1.0 (+{settings.public_site_url})"
    assert "LLU" not in REQUEST_HEADERS["User-Agent"]
