from bot.config import Settings


def test_out_of_scope_features_default_off():
    """These four flags gate the 17lands leaderboard and its sync ticks, disabled by Phase 6 of the
    generalize plan. Upstream (LLU) runs with these on, so a naive rebase merge that takes upstream's
    side of a conflict in bot/config.py silently reactivates the whole leaderboard feature set."""
    settings = Settings(_env_file=None, database_url="postgresql://u:p@localhost/db")

    assert settings.leaderboard_enabled is False
    assert settings.auto_refresh_enabled is False
    assert settings.profile_sync_enabled is False
    assert settings.media_sync_enabled is False
