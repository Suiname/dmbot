from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import Session

from bot import audit, emojis
from bot.config import settings
from bot.commands import descriptions as desc
from bot.commands import token_messages as tmsg
from bot.commands.leaderboard import (
    process_leaderboard,
    render_embed as render_lb,
    render_view as render_lb_view,
)
from bot.commands.messages import MSG_JOINED_LEADERBOARD, MSG_RANKED_AGAIN
from bot.commands.stats import LeaderboardVisibilityView
from bot.services.player_stats import process_stats, render_embed as render_stats_embed
from bot.database import SessionLocal
from bot.discord_helpers import extract_avatar_hash
from bot.models import Player
from bot.services import bot_log
from bot.services.dm_flows import run_latest_flow, send_token_instructions, wait_for_token_reply
from bot.services.pod_replays import schedule_recent_pod_replay_capture
from bot.services.refresh import refresh_one_player_for_all_sets
from bot.services.seventeenlands import SeventeenLandsClient
from bot.services.token_link import link_token, outcome_log_suffix

logger = logging.getLogger(__name__)

DM_TIMEOUT_S = 10 * 60

INSTRUCTIONS = (
    "{hello}**Welcome to the {community} Leaderboard!**\n"
    "Join by sharing your **17lands profile link**.\n"
    + tmsg.WALKTHROUGH_STEPS + "\n"
    "\n"
    + tmsg.TOKEN_PRIVACY_NOTE
)

MSG_DM_SENT = "📬 Check your DMs to join!"
MSG_ALREADY_SIGNED_UP = "You're already in! 🎉 Run `/help` to see everything you can do."
MSG_WELCOME_BACK = "👋 Welcome back! You're on the leaderboard again."
MSG_SYNCING = "⏳ Catching up on your latest 17lands drafts…"
MSG_TIMEOUT = "⏱️ Timed out. Run `/join` again whenever you're ready."


SignupKind = Literal[
    "created",
    "already_signed_up",
    "invalid_format",
    "rejected_by_17lands",
    "token_in_use",
]

SignupCheckKind = Literal["fresh", "reactivated", "already_signed_up", "needs_token", "opted_in"]


@dataclass
class SignupResult:
    kind: SignupKind
    player_id: str | None = None


@dataclass
class SignupCheck:
    kind: SignupCheckKind
    player_id: str | None = None


def check_signup_eligibility(
    session: Session,
    discord_id: str,
    avatar_hash: str | None = None,
) -> SignupCheck:
    """Decide whether the caller needs the full DM flow, a reactivation, or nothing.

    A signed-out player (active=False) gets flipped back to active=True and
    returns "reactivated" — no DM dance needed since their token is still good.
    Avatar hash is refreshed on every reactivation so a long-absent player gets
    their current avatar picked up automatically.
    """
    existing = session.execute(
        select(Player).where(Player.discord_id == discord_id)
    ).scalar_one_or_none()
    if existing is None:
        return SignupCheck(kind="fresh")
    if not existing.active:
        existing.active = True
        if avatar_hash is not None and existing.avatar_hash != avatar_hash:
            existing.avatar_hash = avatar_hash
        session.commit()
        if not existing.seventeenlands_token:
            return SignupCheck(kind="needs_token", player_id=existing.id)
        return SignupCheck(kind="reactivated", player_id=existing.id)
    if not existing.seventeenlands_token:
        return SignupCheck(kind="needs_token", player_id=existing.id)
    if not existing.leaderboard_opt_in:
        existing.leaderboard_opt_in = True
        session.commit()
        return SignupCheck(kind="opted_in", player_id=existing.id)
    return SignupCheck(kind="already_signed_up", player_id=existing.id)


def process_signup(
    session: Session,
    client: SeventeenLandsClient,
    discord_id: str,
    discord_username: str,
    display_name: str,
    token_input: str,
    avatar_hash: str | None = None,
) -> SignupResult:
    existing_by_discord = session.execute(
        select(Player).where(Player.discord_id == discord_id)
    ).scalar_one_or_none()
    if existing_by_discord is not None:
        return SignupResult(kind="already_signed_up", player_id=existing_by_discord.id)

    result = link_token(
        session, client, discord_id, discord_username, display_name, token_input, avatar_hash, opt_in=True,
    )
    if result.kind == "linked":
        return SignupResult(kind="created", player_id=result.player_id)
    return SignupResult(kind=result.kind, player_id=result.player_id)


class Signup(commands.Cog):
    def __init__(self, bot: commands.Bot, client: SeventeenLandsClient | None = None) -> None:
        self.bot = bot
        self.client = client or SeventeenLandsClient()

    @app_commands.command(name="join", description=desc.JOIN)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.allowed_installs(guilds=True, users=False)
    async def signup(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)
        username = str(interaction.user)
        audit.event("signup_invoked", user_id=user_id, username=username)

        logger.info(f"join: {username} invoked")
        await run_latest_flow(user_id, self._run_signup_flow(interaction, user_id, username))

    async def _run_signup_flow(
        self, interaction: discord.Interaction, user_id: str, username: str,
    ) -> None:
        avatar_hash = extract_avatar_hash(interaction.user)

        with SessionLocal() as session:
            check = check_signup_eligibility(session, user_id, avatar_hash=avatar_hash)
        if check.kind in ("reactivated", "opted_in"):
            reactivated = check.kind == "reactivated"
            audit.event(f"signup_{check.kind}", user_id=user_id, player_id=check.player_id)
            logger.info(f"join: {username} {check.kind}")
            in_guild = interaction.guild is not None
            await interaction.response.defer(ephemeral=in_guild, thinking=True)
            # Only reactivated players need a catch-up pull; opted-in players stayed in the periodic refresh
            progress = None
            if reactivated:
                progress = await interaction.followup.send(MSG_SYNCING, ephemeral=in_guild, wait=True)
                with SessionLocal() as session:
                    refresh_one_player_for_all_sets(session, self.client, check.player_id)
                    session.commit()
            await bot_log.get(self.bot).post_plain(
                f"🔁 **{interaction.user.display_name}** rejoined the leaderboard"
            )
            back_msg = MSG_WELCOME_BACK if reactivated else MSG_RANKED_AGAIN
            stats_embed, stats_view = await _build_join_stats(self.bot, user_id)
            await _deliver_rejoin_reply(interaction, progress, back_msg, stats_embed, stats_view, in_guild)
            return
        if check.kind == "already_signed_up":
            audit.event("signup_short_circuit", user_id=user_id, reason="already_signed_up")
            await interaction.response.send_message(MSG_ALREADY_SIGNED_UP, ephemeral=(interaction.guild is not None))
            return

        # Defer first — uploading the walkthrough image can blow past the 3s response deadline
        in_guild = interaction.guild is not None
        await interaction.response.defer(ephemeral=in_guild, thinking=True)
        try:
            dm = await interaction.user.create_dm()
            if in_guild:
                await _send_signup_instructions(dm.send)
                await interaction.followup.send(MSG_DM_SENT, ephemeral=True)
            else:
                await _send_signup_instructions(interaction.followup.send)
        except discord.Forbidden:
            audit.event("signup_dms_disabled", user_id=user_id, username=username)
            logger.warning(f"join: {username} DMs blocked")
            await interaction.followup.send(tmsg.DMS_DISABLED, ephemeral=in_guild)
            return
        audit.event("signup_dm_sent", user_id=user_id)

        reply_text = await wait_for_token_reply(self.bot, interaction, timeout_s=DM_TIMEOUT_S)
        if reply_text is None:
            audit.event("signup_timeout", user_id=user_id, username=username)
            logger.info(f"join: {username} timed out")
            await dm.send(MSG_TIMEOUT)
            return

        # Length only — never log the raw token content
        audit.event("signup_dm_reply_received", user_id=user_id, reply_length=len(reply_text))
        await dm.send(tmsg.CHECKING)

        with SessionLocal() as session:
            result = link_token(
                session, self.client, user_id, username,
                interaction.user.display_name, reply_text, avatar_hash, opt_in=True,
            )

        audit.event(
            "signup_result",
            user_id=user_id,
            username=username,
            kind=result.kind,
            player_id=result.player_id,
        )
        logger.info(f"join: {username} → {result.kind} {outcome_log_suffix(result.kind, reply_text)}")

        if result.kind == "invalid_format":
            await dm.send(tmsg.INVALID_FORMAT)
            return
        if result.kind == "rejected_by_17lands":
            await dm.send(tmsg.REJECTED)
            return
        if result.kind == "token_in_use":
            await dm.send(tmsg.TOKEN_IN_USE)
            return

        # linked — pull fresh stats so they show up immediately
        await dm.send(tmsg.FETCHING_EVENTS)
        with SessionLocal() as session:
            refresh_one_player_for_all_sets(session, self.client, result.player_id)
            session.commit()
        schedule_recent_pod_replay_capture(result.player_id, self.client)
        await bot_log.get(self.bot).post_plain(
            f"🆕 **{interaction.user.display_name}** joined the leaderboard"
        )
        await dm.send(MSG_JOINED_LEADERBOARD)

        # Show the leaderboard right here in DM, plus the personal stats
        # breakdown — same pair the /leaderboard command sends to its invoker
        try:
            lb_embed, lb_view, stats_embed, stats_view = await _build_join_preview(self.bot, user_id)
            if lb_embed is not None:
                await dm.send(embed=lb_embed, view=lb_view)
            if stats_embed is not None:
                await dm.send(embed=stats_embed, view=stats_view)
        except Exception:
            logger.warning("post-join leaderboard/stats preview failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Signup(bot))


async def _send_signup_instructions(send) -> None:
    content = INSTRUCTIONS.format(hello=emojis.prefix("chordoHello"), community=settings.community_name)
    await send_token_instructions(send, content)


async def _build_join_stats(bot: commands.Bot, user_id: str):
    """Personal stats embed + Hide/Show-rank view for a joined or reactivated user.

    Either element is None when there's nothing to show yet (no stats this set, or
    no token to gate the visibility toggle on).
    """
    with SessionLocal() as session:
        stats_data = process_stats(session, player_name=None, viewer_discord_id=user_id)
    if stats_data is None:
        return None, None
    stats_view = LeaderboardVisibilityView(bot, user_id, stats_data) if stats_data.has_token else None
    return render_stats_embed(stats_data), stats_view


async def _build_join_preview(bot: commands.Bot, user_id: str):
    """Leaderboard + personal stats for a freshly-linked user's welcome DM.

    Returns (leaderboard_embed, leaderboard_view, stats_embed, stats_view); any
    element may be None when there's nothing to show.
    """
    with SessionLocal() as session:
        lb_data = process_leaderboard(session, viewer_discord_id=user_id)
    lb_embed = render_lb(lb_data) if lb_data is not None else None
    stats_embed, stats_view = await _build_join_stats(bot, user_id)
    return lb_embed, render_lb_view(), stats_embed, stats_view


async def _deliver_rejoin_reply(interaction, progress, content, stats_embed, stats_view, ephemeral):
    """Send the rejoin confirmation plus personal stats in the invoked channel.

    Edits the in-progress sync message in place when one was posted, otherwise sends
    a fresh followup. Pod-only players with no stats this set just get the text.
    """
    kwargs = {"content": content}
    if stats_embed is not None:
        kwargs["embed"] = stats_embed
    if stats_view is not None:
        kwargs["view"] = stats_view
    if progress is not None:
        await progress.edit(**kwargs)
    else:
        await interaction.followup.send(ephemeral=ephemeral, **kwargs)
