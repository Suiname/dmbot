"""Shared 17lands token-linking DM flow behind /link-17lands and the pod RSVP Link 17Lands button.

One entry point runs the whole flow: DM the walkthrough, wait for the token reply, link it, pull a
first stats sync, backfill any recent pod's replays in the background, then offer the leaderboard
opt-in. Both callers reach it so the copy and the steps never drift.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot import audit
from bot import emojis
from bot.config import settings
from bot.commands import token_messages as tmsg
from bot.commands.messages import MSG_JOINED_LEADERBOARD
from bot.database import SessionLocal
from bot.discord_helpers import extract_avatar_hash
from bot.models import Player
from bot.services import bot_log
from bot.services.dm_flows import run_latest_flow, send_token_instructions, wait_for_token_reply
from bot.services.player_stats import process_stats, render_embed as render_stats_embed
from bot.services.pod_replays import schedule_recent_pod_replay_capture
from bot.services.refresh import refresh_one_player_for_all_sets
from bot.services.seventeenlands import SeventeenLandsClient
from bot.services.token_link import link_token, outcome_log_suffix

logger = logging.getLogger(__name__)

DM_TIMEOUT_S = 10 * 60
PROMPT_TIMEOUT_S = 10 * 60

LEADERBOARD_URL = settings.leaderboard_url

INSTRUCTIONS = (
    "**Link your 17lands profile** to track your games.\n"
    + tmsg.WALKTHROUGH_STEPS + "\n"
    "\n"
    + tmsg.TOKEN_PRIVACY_NOTE
)

MSG_DM_SENT = "📬 Check your DMs to finish linking 17lands."
MSG_TIMEOUT = "⏱️ Timed out. Run `/link-17lands` whenever you're ready to try again."
MSG_LINK_OFF_BOARD = f"17lands linked! Want to join the [live leaderboard](<{LEADERBOARD_URL}>)?"
MSG_LINK_ON_BOARD = "updated!"
MSG_LEFT = "👋 You've left the leaderboard. Your games still count for pods — run `/join` anytime to return."
MSG_STAYED_OFF = "👍 You're off the leaderboard. Your games are still tracked for pods, and you can `/join` anytime."
MSG_STAYED_ON = "👍 You're still on the leaderboard."


async def start_link_17lands_flow(
    bot: commands.Bot, interaction: discord.Interaction, client: SeventeenLandsClient | None = None,
) -> None:
    """Run the token-linking DM flow, newest invocation winning if the same user starts it twice."""
    pull_client = client or SeventeenLandsClient()
    user_id = str(interaction.user.id)
    username = str(interaction.user)
    await run_latest_flow(user_id, _run_link_flow(bot, pull_client, interaction, user_id, username))


async def _run_link_flow(
    bot: commands.Bot, client: SeventeenLandsClient, interaction: discord.Interaction,
    user_id: str, username: str,
) -> None:
    in_guild = interaction.guild is not None
    await interaction.response.defer(ephemeral=in_guild, thinking=True)
    try:
        dm = await interaction.user.create_dm()
        if in_guild:
            await send_token_instructions(dm.send, INSTRUCTIONS)
            await interaction.followup.send(MSG_DM_SENT, ephemeral=True)
        else:
            await send_token_instructions(interaction.followup.send, INSTRUCTIONS)
    except discord.Forbidden:
        audit.event("link_17lands_dms_disabled", user_id=user_id)
        logger.warning(f"link-17lands: {username} DMs blocked")
        await interaction.followup.send(tmsg.DMS_DISABLED, ephemeral=in_guild)
        return

    audit.event("link_17lands_dm_sent", user_id=user_id)

    reply_text = await wait_for_token_reply(bot, interaction, timeout_s=DM_TIMEOUT_S)
    if reply_text is None:
        audit.event("link_17lands_timeout", user_id=user_id)
        logger.info(f"link-17lands: {username} timed out")
        await dm.send(MSG_TIMEOUT)
        return

    await dm.send(tmsg.CHECKING)

    with SessionLocal() as session:
        result = link_token(
            session, client, user_id, username,
            interaction.user.display_name, reply_text, extract_avatar_hash(interaction.user),
            opt_in=False,
        )

    audit.event("link_17lands_result", user_id=user_id, kind=result.kind, player_id=result.player_id)
    logger.info(f"link-17lands: {username} → {result.kind} {outcome_log_suffix(result.kind, reply_text)}")

    if result.kind == "invalid_format":
        await dm.send(tmsg.INVALID_FORMAT)
        return
    if result.kind == "rejected_by_17lands":
        await dm.send(tmsg.REJECTED)
        return
    if result.kind == "token_in_use":
        await dm.send(tmsg.TOKEN_IN_USE)
        return

    await dm.send(tmsg.FETCHING_EVENTS)
    with SessionLocal() as session:
        refresh_one_player_for_all_sets(session, client, result.player_id)
        session.commit()
    schedule_recent_pod_replay_capture(result.player_id, client)

    with SessionLocal() as session:
        player = session.get(Player, result.player_id)
        currently_in = bool(player and player.active and player.leaderboard_opt_in)

    stats_embed = await _stats_card_embed(user_id)
    view = LeaderboardChoicePrompt(bot, user_id, result.player_id, currently_in=currently_in)
    send_kwargs = {"content": link_prompt(currently_in), "view": view}
    if stats_embed is not None:
        send_kwargs["embed"] = stats_embed
    view.message = await dm.send(**send_kwargs)


def link_prompt(currently_in: bool) -> str:
    icon = emojis.get("17lands") or "✅"
    return f"{icon} {MSG_LINK_ON_BOARD if currently_in else MSG_LINK_OFF_BOARD}"


async def _stats_card_embed(user_id: str) -> discord.Embed | None:
    """The player's personal stats card for the current set, or None when they have nothing to show."""
    def _build() -> discord.Embed | None:
        with SessionLocal() as session:
            data = process_stats(session, player_name=None, viewer_discord_id=user_id)
        return render_stats_embed(data) if data is not None else None

    return await asyncio.to_thread(_build)


class LeaderboardChoicePrompt(discord.ui.View):
    """Post-link choice that lets a player join or leave the leaderboard from whichever state they're in."""

    def __init__(self, bot: commands.Bot, user_id: str, player_id: str, *, currently_in: bool) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT_S)
        self.bot = bot
        self.user_id = user_id
        self.player_id = player_id
        self.currently_in = currently_in
        self.message: discord.Message | None = None

        if currently_in:
            self._add_button("Leave the Leaderboard", discord.ButtonStyle.danger, self._leave)
            self._add_button("Stay On", discord.ButtonStyle.secondary, self._keep)
        else:
            self._add_button(
                "Join the Leaderboard", discord.ButtonStyle.success, self._join,
                emoji=emojis.get_emoji("llu"),
            )
            self._add_button("Stay Off", discord.ButtonStyle.secondary, self._keep)

    def _add_button(self, label: str, style: discord.ButtonStyle, callback, *, emoji=None) -> None:
        button = discord.ui.Button(label=label, style=style, emoji=emoji)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _join(self, interaction: discord.Interaction) -> None:
        display_name = self._set_membership(active=True, opt_in=True)
        audit.event("link_17lands_join", user_id=self.user_id, player_id=self.player_id)
        await self._finish(interaction, MSG_JOINED_LEADERBOARD)
        if display_name:
            await bot_log.get(self.bot).post_plain(f"🆕 **{display_name}** joined the leaderboard")

    async def _leave(self, interaction: discord.Interaction) -> None:
        display_name = self._set_membership(opt_in=False)
        audit.event("link_17lands_leave", user_id=self.user_id, player_id=self.player_id)
        await self._finish(interaction, MSG_LEFT)
        if display_name:
            await bot_log.get(self.bot).post_plain(f"👋 **{display_name}** left the leaderboard")

    async def _keep(self, interaction: discord.Interaction) -> None:
        audit.event("link_17lands_no_change", user_id=self.user_id, player_id=self.player_id)
        await self._finish(interaction, MSG_STAYED_ON if self.currently_in else MSG_STAYED_OFF)

    async def _finish(self, interaction: discord.Interaction, content: str) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    def _set_membership(self, *, opt_in: bool, active: bool | None = None) -> str | None:
        with SessionLocal() as session:
            player = session.get(Player, self.player_id)
            if player is None:
                return None
            player.leaderboard_opt_in = opt_in
            if active is not None:
                player.active = active
            display_name = player.display_name
            session.commit()
        return display_name
