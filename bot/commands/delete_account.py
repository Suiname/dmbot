from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bot import audit
from bot.config import settings
from bot.commands import descriptions as desc
from bot.commands.messages import MSG_NOT_ON_BOARD
from bot.database import SessionLocal
from bot.models import DraftEvent, Player, PlayerStats
from bot.services import bot_log

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT_S = 5 * 60

MSG_CONFIRM = (
    "⚠️ This will delete all your tracked stats and remove you from the {community} leaderboard. "
    "Your 17lands data is unaffected."
)
MSG_DELETED = "You've been removed from the {community} leaderboard. Run `/join` anytime to come back."
MSG_CANCELLED = "Deletion canceled."


DeleteAccountKind = Literal["deleted", "not_registered"]


@dataclass
class DeleteAccountResult:
    kind: DeleteAccountKind
    deleted_player_id: str | None = None


def process_delete_account(session: Session, discord_id: str) -> DeleteAccountResult:
    """Scrub the player's leaderboard footprint, keeping the row so pod-draft history still references them.

    Deletes tracked stats + draft events, clears the 17lands link, and drops them
    from the leaderboard. The player row, discord_id, and pod participations stay.
    """
    player = session.execute(
        select(Player).where(Player.discord_id == discord_id)
    ).scalar_one_or_none()
    if player is None:
        return DeleteAccountResult(kind="not_registered")
    player_id = player.id
    session.execute(delete(PlayerStats).where(PlayerStats.player_id == player_id))
    session.execute(delete(DraftEvent).where(DraftEvent.player_id == player_id))
    player.seventeenlands_token = None
    player.token_invalid = False
    player.active = False
    player.leaderboard_opt_in = False
    session.commit()
    return DeleteAccountResult(kind="deleted", deleted_player_id=player_id)


class ConfirmExileView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: str) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_S)
        self.bot = bot
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id

    @discord.ui.button(label="Yes, delete my data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        with SessionLocal() as session:
            result = process_delete_account(session, self.user_id)
        audit.event(
            "delete_account_result", user_id=self.user_id, kind=result.kind,
            deleted_player_id=result.deleted_player_id,
        )
        logger.info(f"exile: {interaction.user} deleted player_id={result.deleted_player_id}")
        if result.kind == "deleted":
            await bot_log.get(self.bot).post_plain(
                f"🚪 **{interaction.user.display_name}** exiled from the leaderboard"
            )
        await interaction.response.edit_message(
            content=MSG_DELETED.format(community=settings.community_name) if result.kind == "deleted" else MSG_NOT_ON_BOARD, view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        audit.event("delete_account_declined", user_id=self.user_id)
        await interaction.response.edit_message(content=MSG_CANCELLED, view=None)
        self.stop()


class DeleteAccount(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="exile", description=desc.EXILE)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.allowed_installs(guilds=True, users=False)
    async def delete_account(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)
        username = str(interaction.user)
        ephemeral = interaction.guild is not None
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        audit.event("delete_account_invoked", user_id=user_id, username=username)
        logger.info(f"exile: {username} invoked")

        with SessionLocal() as session:
            existing = session.execute(
                select(Player).where(Player.discord_id == user_id)
            ).scalar_one_or_none()
        if existing is None:
            audit.event("delete_account_short_circuit", user_id=user_id, reason="not_registered")
            logger.info(f"exile: {username} not registered")
            await interaction.followup.send(MSG_NOT_ON_BOARD, ephemeral=ephemeral)
            return

        await interaction.followup.send(
            MSG_CONFIRM.format(community=settings.community_name), view=ConfirmExileView(self.bot, user_id), ephemeral=ephemeral,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeleteAccount(bot))
