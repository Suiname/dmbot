from __future__ import annotations

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DRAFTMANCER_HOST = "draftmancer.com"


class Settings(BaseSettings):
    """Process-wide configuration loaded from env (or .env in repo root).

    Discord fields are optional so non-bot entry points (alembic CLI, the
    seed script, tests) can construct Settings without them.

    The active set is derived from today's date in ``bot/sets.py``, not here —
    rotation needs no config change.
    """
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    discord_bot_token: SecretStr | None = None
    discord_guild_id: int | None = None
    discord_admin_role_id: int | None = None
    production_guild_id: int | None = None

    @model_validator(mode="after")
    def _default_production_guild_id(self) -> "Settings":
        if self.production_guild_id is None and self.discord_guild_id is not None:
            self.production_guild_id = self.discord_guild_id
        return self
    discord_botlog_channel_id: int | None = None
    feedback_channel_id: int = 1504825374188507156
    preview_season_channel_id: int | None = None
    community_name: str = "this server"
    public_site_url: str = ""
    auto_refresh_enabled: bool = False
    leaderboard_enabled: bool = False

    @property
    def leaderboard_url(self) -> str:
        return f"{self.public_site_url.rstrip('/')}/leaderboard"

    @property
    def player_base_url(self) -> str:
        return f"{self.public_site_url.rstrip('/')}/player"

    scribe_cache_bust: bool = False
    format_schedule_enabled: bool = True

    pod_draft_channel_id: int = 1028072146645295125
    discord_botlog_channel_name: str = "bot-spam"
    pod_draft_chat_channel_name: str = "pod-draft-chat"
    pod_draft_trophy_hype_channel_name: str = "trophy-hype"
    pod_draft_target_players: int = 8
    pod_draft_voice_channel_name: str = "Pod General"
    pod_schedule_enabled: bool = True
    pod_draft_session_prefix: str = "LLU"
    pod_draft_max_players: int = 8
    pod_draft_min_ready_players: int = 6
    pod_table_open_threshold: int = 4
    pod_round_robin_size: int = 4
    pod_round_robin_offer_delay_s: int = 120
    pod_draft_pick_timer: int = 60
    pod_draft_picks_per_pack: int = 1
    pod_draft_bots: int = 0
    pod_draft_fallback_tz: str = "America/New_York"
    pod_draft_skip_reminder_wait: bool = False
    pod_draft_end_watchdog_minutes: int = 90
    pod_signal_fire_threshold: int = 6
    pod_queue_inactivity_minutes: int = 180
    pod_underfill_check_hours: str = "3,2,1"
    pod_underfill_ping_hours: str = "1"
    pod_underfill_ping_close_gap: int = 2

    @property
    def pod_underfill_check_hours_tuple(self) -> tuple[int, ...]:
        return _int_csv(self.pod_underfill_check_hours)

    @property
    def pod_underfill_ping_hours_set(self) -> frozenset[int]:
        return frozenset(_int_csv(self.pod_underfill_ping_hours))

    draftmancer_ws_url: str = f"wss://{DRAFTMANCER_HOST}"
    draftmancer_web_url: str = f"https://{DRAFTMANCER_HOST}"

    youtube_api_key: SecretStr | None = None
    youtube_channel_handle: str = ""
    libsyn_feed_url: str = ""
    podcast_title_prefix: str | None = None
    media_sync_enabled: bool = False
    profile_sync_enabled: bool = False


def _int_csv(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.split(",") if part.strip())


settings = Settings()
