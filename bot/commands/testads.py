"""Owner-only `!test ads` — post every `!championship` and `!p0p1` variation through the real builders."""
from __future__ import annotations

from datetime import datetime, timedelta

import discord
from discord import ui
from discord.ext import commands

from bot.commands.test_group import test_group
from bot.config import settings
from bot.services import championship, p0p1_contest, p0p1_copy
from bot.services import championship_copy as cc
from bot.services.containers import as_view
from bot.services.ping_roles import (
    SET_CHAMPION_ROLE_NAME,
    TOP_P0P1_CHALLENGER_ROLE_NAME,
    champion_role_mention,
)
from bot.services.pod_roles import find_role
from bot.services.pod_schedule import SCHEDULE_TZ

CARD_URL = f"https://discord.com/channels/{settings.production_guild_id}/000/000"


async def setup(bot: commands.Bot) -> None:
    @test_group.command(name="ads")
    @commands.is_owner()
    async def test_ads(ctx: commands.Context) -> None:
        """Owner-only. Post the championship explainer in each state and the P0P1 ad in each phase."""
        for title, container in _championship_variations(ctx):
            await ctx.send(view=as_view(ui.TextDisplay(f"__**{title}**__"), container),
                           allowed_mentions=discord.AllowedMentions.none())
        contest = p0p1_contest.contest_to_advertise(datetime.now(SCHEDULE_TZ))
        if contest is None:
            await ctx.send("No P0P1 contest in `p0p1_contests.json`.")
            return
        for title, when in _p0p1_moments(contest):
            featured = p0p1_contest.featured_contest(when)
            container = p0p1_copy.advertisement(
                contest, when, featured_code=featured.code if featured is not None else None,
                challenger_mention=p0p1_copy.challenger_mention(
                    find_role(ctx.guild, TOP_P0P1_CHALLENGER_ROLE_NAME)),
            )
            await ctx.send(view=as_view(ui.TextDisplay(f"__**{title}**__"), container),
                           allowed_mentions=discord.AllowedMentions.none())


def _championship_variations(ctx: commands.Context) -> list[tuple[str, ui.Container]]:
    plan = championship.plan_for(datetime.now(SCHEDULE_TZ))
    set_code = plan.set_code if plan is not None else "MSH"
    event_at = plan.event_at if plan is not None else datetime.now(SCHEDULE_TZ) + timedelta(days=5)
    champion_mention = champion_role_mention(find_role(ctx.guild, SET_CHAMPION_ROLE_NAME))
    signup_at = championship.signup_post_at(plan) if plan is not None else None
    states = [
        ("championship: signup card posted", event_at, signup_at, CARD_URL),
        ("championship: signup card still to come", event_at, signup_at, None),
        ("championship: between editions", None, None, None),
    ]
    variations = []
    for title, when, signup, card_url in states:
        variations.append((title, cc.explainer(
            set_code=set_code, event_at=when, signup_at=signup, champion_mention=champion_mention,
            card_url=card_url, coordination_channel=f"<#{settings.pod_draft_channel_id}>",
        )))
    return variations


def _p0p1_moments(contest: p0p1_contest.Contest) -> list[tuple[str, datetime]]:
    return [
        ("p0p1: before it opens", contest.previews_open - timedelta(days=1)),
        ("p0p1: open for voting", contest.previews_open + timedelta(hours=1)),
        ("p0p1: closed, waiting on results", contest.voting_deadline + timedelta(days=1)),
        ("p0p1: final standings", contest.scoring_date + timedelta(hours=1)),
    ]
