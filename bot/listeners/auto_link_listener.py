"""Auto-link a 17lands URL/token pasted in DM, no slash command required.

Routes the message through the same primitives `/join` and `/link-17lands` use:
- no Player row → process_signup,
- inactive Player → check_signup_eligibility reactivates + link_token swaps token,
- active Player with different token → link_token (re-link),
- active Player with same token → silent ack.

Defers to an active /join or /link-17lands wait_for via dm_flows' shared in-flight set.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands
from sqlalchemy import select

from bot import audit, emojis
from bot.config import settings
from bot.commands import token_messages as tmsg
from bot.commands.messages import MSG_JOINED_LEADERBOARD
from bot.commands.signup import (
    MSG_WELCOME_BACK,
    check_signup_eligibility,
    process_signup,
)
from bot.database import SessionLocal
from bot.discord_helpers import extract_avatar_hash
from bot.models import Player
from bot.services import bot_log
from bot.services.dm_flows import dm_flow, is_in_flight
from bot.services.refresh import refresh_one_player_for_all_sets
from bot.services.seventeenlands import SeventeenLandsClient, extract_token
from bot.services.token_link import link_token

log = logging.getLogger(__name__)

MSG_ALREADY_LINKED = "That token is already linked to your account — use `/leaderboard` to see your stats."
MSG_UPDATED = "Updated. Your latest stats are on the [leaderboard]({url})"
MSG_UPDATED_HIDDEN = "Updated. Your rank stays hidden. Run `/join` to appear on the [leaderboard]({url})"


def updated_message(opted_in: bool) -> str:
    icon = emojis.get("17lands") or "✅"
    msg = MSG_UPDATED if opted_in else MSG_UPDATED_HIDDEN
    return f"{icon} {msg.format(url=settings.leaderboard_url)}"


class AutoLinkListener(commands.Cog):
    def __init__(self, bot: commands.Bot, client: SeventeenLandsClient | None = None) -> None:
        self.bot = bot
        self.client = client or SeventeenLandsClient()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        user_id = str(message.author.id)
        if is_in_flight(user_id):
            return
        try:
            token = extract_token(message.content)
        except ValueError:
            return

        username = str(message.author)
        log.info(f"auto-link: {username} pasted URL [token=…{token[-4:]}]")
        audit.event("auto_link_detected", user_id=user_id, username=username)

        with dm_flow(user_id):
            await self._route(message, user_id, username, token)

    async def _route(
        self,
        message: discord.Message,
        user_id: str,
        username: str,
        token: str,
    ) -> None:
        avatar_hash = extract_avatar_hash(message.author)

        with SessionLocal() as session:
            player = session.execute(
                select(Player).where(Player.discord_id == user_id)
            ).scalar_one_or_none()

        if player is None:
            await self._handle_signup(message, user_id, username, avatar_hash)
            return

        if player.active and player.seventeenlands_token == token:
            log.info(f"auto-link: {username} → no-op same token")
            await _safe_dm(message.author, MSG_ALREADY_LINKED)
            return

        if not player.active:
            with SessionLocal() as session:
                check_signup_eligibility(session, user_id, avatar_hash=avatar_hash)
            audit.event("auto_link_reactivated", user_id=user_id)

        await self._handle_relink(message, user_id, username, avatar_hash, was_inactive=not player.active)

    async def _handle_signup(
        self,
        message: discord.Message,
        user_id: str,
        username: str,
        avatar_hash: str | None,
    ) -> None:
        with SessionLocal() as session:
            result = process_signup(
                session=session,
                client=self.client,
                discord_id=user_id,
                discord_username=username,
                display_name=message.author.display_name,
                token_input=message.content,
                avatar_hash=avatar_hash,
            )
        log.info(f"auto-link: {username} → {result.kind}")
        audit.event(
            "auto_link_signup_result",
            user_id=user_id,
            username=username,
            kind=result.kind,
            player_id=result.player_id,
        )

        if result.kind == "created":
            with SessionLocal() as session:
                refresh_one_player_for_all_sets(session, self.client, result.player_id)
                session.commit()
            await bot_log.get(self.bot).post_plain(
                f"🆕 **{message.author.display_name}** joined the leaderboard"
            )
            await _safe_dm(message.author, MSG_JOINED_LEADERBOARD)
        elif result.kind == "token_in_use":
            await _safe_dm(message.author, tmsg.TOKEN_IN_USE)
        elif result.kind == "rejected_by_17lands":
            await _safe_dm(message.author, tmsg.REJECTED)
        elif result.kind == "invalid_format":
            await _safe_dm(message.author, tmsg.INVALID_FORMAT)

    async def _handle_relink(
        self,
        message: discord.Message,
        user_id: str,
        username: str,
        avatar_hash: str | None,
        was_inactive: bool,
    ) -> None:
        with SessionLocal() as session:
            result = link_token(
                session, self.client, user_id, username,
                message.author.display_name, message.content, avatar_hash, opt_in=True,
            )
        log.info(f"auto-link: {username} → relink {result.kind}")
        audit.event("auto_link_relink_result", user_id=user_id, kind=result.kind, player_id=result.player_id)

        if result.kind == "linked":
            with SessionLocal() as session:
                refresh_one_player_for_all_sets(session, self.client, result.player_id)
                session.commit()
            if was_inactive:
                await bot_log.get(self.bot).post_plain(
                    f"🔁 **{message.author.display_name}** rejoined the leaderboard"
                )
                await _safe_dm(message.author, MSG_WELCOME_BACK)
            else:
                with SessionLocal() as session:
                    player = session.get(Player, result.player_id)
                    opted_in = bool(player and player.leaderboard_opt_in)
                await _safe_dm(message.author, updated_message(opted_in))
        elif result.kind == "token_in_use":
            await _safe_dm(message.author, tmsg.TOKEN_IN_USE)
        elif result.kind == "rejected_by_17lands":
            await _safe_dm(message.author, tmsg.REJECTED)


async def _safe_dm(user: discord.abc.User, content: str) -> None:
    try:
        await user.send(content)
    except discord.HTTPException:
        log.warning(f"auto-link: could not DM {user}", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoLinkListener(bot))
