"""`/pod-schedule` — the calendar of which formats the pods draft over the weeks ahead.

The grid ships as a rendered PNG (`pod_schedule_image`); everything a reader might want to select, click or
localize stays text. An embed rather than Components V2: mobile Pins, search results and reply previews
render content and embeds only, so a V2 layout shows as an empty message everywhere it is previewed. The
cost is the set-release note, which an embed can only place above the calendar it annotates.

The heading opens the description instead of filling `title`, which takes no markdown: only the body can
carry a heading level, and only there does the link stop exactly where its text does.

The slot line reads off the poll buckets and answers with each slot's next start, so a call after midnight
names today's pods without the reader converting anything out of ET.
"""
from __future__ import annotations

import asyncio
import io
from datetime import date, datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot import audit
from bot.commands import descriptions as desc
from bot.config import settings
from bot.discord_helpers import EM_SPACE, posts_publicly
from bot.services import championship
from bot.services import pod_format_interest as fi
from bot.services.ping_roles import SET_CHAMPION_ROLE_NAME
from bot.services.pod_format import is_custom
from bot.services.pod_format_schedule import calendar_days, extras_on, latest_on, rotation_in
from bot.services.pod_roles import role_mention
from bot.services.pod_schedule import SCHEDULE_TZ
from bot.services.pod_schedule_image import render_calendar_png
from bot.services.pod_signals import WEEKDAY_BUCKETS, is_weekend, next_lane_start
from bot.sets import active_set_code, release_instant, set_name_for

MSG_HEADING = "## 🗓️ [Pod Draft Format Schedule]({url}) 🚀"
MSG_SLOT = "{emoji} {role} **<t:{unix}:t>**"
MSG_DAILY_SET = "{symbol} {role} **every day**"
MSG_EXTRA_FORMAT = "{symbol} {role} **{days}**"
MSG_EXTRA_FORMAT_ANY_DAY = "{symbol} {role}"
DAYS_WEEKDAY = "Mon-Fri"
DAYS_WEEKEND = "Weekends"
MSG_CHAMPIONSHIP = "👑 {role} <t:{unix}:R>"
MSG_ARRIVAL = "{symbol} **{name}** <t:{unix}:R>"

CHANNEL_URL = "https://discord.com/channels/{guild_id}/{channel_id}"

IMAGE_FILENAME = "pod-schedule.png"
IMAGE_URL = f"attachment://{IMAGE_FILENAME}"
DEFAULT_WEEKS = 4
COLUMN_GAP = EM_SPACE * 2
SET_COLUMN_GAP = EM_SPACE
LINE_GAP = "\n\n"


class PodSchedule(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="pod-schedule", description=desc.POD_SCHEDULE)
    @app_commands.describe(weeks="How many weeks to show")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.allowed_installs(guilds=True, users=False)
    async def pod_schedule(
        self, interaction: discord.Interaction, weeks: app_commands.Range[int, 1, 6] = DEFAULT_WEEKS,
    ) -> None:
        now = datetime.now(SCHEDULE_TZ)
        audit.event("pod_schedule_invoked", user_id=str(interaction.user.id), weeks=weeks)
        plan = championship.plan_for(now)
        crown = plan.event_at if plan is not None else None
        png = await asyncio.to_thread(render_calendar_png, now.date(), weeks, crown.date() if crown else None)
        await interaction.response.send_message(
            embed=build_schedule_embed(interaction.guild, now, weeks, crown),
            file=discord.File(io.BytesIO(png), IMAGE_FILENAME),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=not posts_publicly(interaction),
        )


def build_schedule_embed(guild: discord.Guild | None, now: datetime, weeks: int,
                         championship_at: datetime | None) -> discord.Embed:
    days = calendar_days(now.date(), weeks)
    lines = [slot_line(guild, now), set_line(guild, days)]
    extras = extras_line(guild, days, now.date())
    if extras:
        lines.append(extras)
    championship = championship_line(guild, days, now, championship_at)
    if championship:
        lines.append(championship)
    heading = MSG_HEADING.format(url=coordination_url(guild))
    embed = discord.Embed(description=f"{heading}\n{LINE_GAP.join(lines)}", color=discord.Color.green())
    embed.set_image(url=IMAGE_URL)
    return embed


def coordination_url(guild: discord.Guild | None) -> str:
    """The heading links to the channel the pods actually run in, which is the one route out of the schedule
    that survives a pin preview: those render the embed but drop any button under it. A DM carries no guild
    of its own, so it lands on the production server."""
    guild_id = guild.id if guild is not None else settings.production_guild_id
    return CHANNEL_URL.format(guild_id=guild_id, channel_id=settings.pod_draft_channel_id)


def slot_line(guild: discord.Guild | None, now: datetime) -> str:
    """Both slots on one row, each naming the role to hold and when it next drafts. The weekday roles carry
    the line: a reader wants the role to pick up, and the weekend variants are the launcher's bookkeeping."""
    slots = []
    for bucket in WEEKDAY_BUCKETS:
        start = next_lane_start(bucket.lane, now)
        if start is None:
            continue
        slots.append(MSG_SLOT.format(
            emoji=bucket.emoji, role=role_mention(guild, bucket.role_name), unix=int(start.timestamp()),
        ))
    return COLUMN_GAP.join(slots)


def set_line(guild: discord.Guild | None, days: list[date]) -> str:
    """The set every pod drafts, and beside it the set arriving inside the rendered span, so both sets a
    reader has to care about sit on one row. The daily set is named by its ping role rather than by code,
    which keeps the line true across a rotation and lets the days after one stay blank on the calendar.

    Its first column runs wider than the rows around it, so it takes a narrower gap to bring its second
    column back under theirs."""
    code = active_set_code()
    columns = [MSG_DAILY_SET.format(
        symbol=fi.format_emoji(code), role=role_mention(guild, fi.LATEST_SET_ROLE_NAME),
    )]
    arrival = rotation_in(days)
    if arrival is not None:
        incoming = latest_on(arrival)
        columns.append(MSG_ARRIVAL.format(
            symbol=fi.format_emoji(incoming), name=set_name_for(incoming),
            unix=int(release_instant(arrival).timestamp()),
        ))
    return SET_COLUMN_GAP.join(columns)


def extras_line(guild: discord.Guild | None, days: list[date], today: date) -> str:
    """The formats that run beside the daily set, in the same columns the slots use. Empty until a set cycle
    has days written for them."""
    items = []
    for role_name, symbol, when in scheduled_extras(days, today):
        template = MSG_EXTRA_FORMAT if when else MSG_EXTRA_FORMAT_ANY_DAY
        items.append(template.format(symbol=symbol, role=role_mention(guild, role_name), days=when))
    return COLUMN_GAP.join(items)


def scheduled_extras(days: list[date], today: date) -> list[tuple[str, object, str]]:
    """The flashback and cube roles that have pods still to come in the rendered span, each with the days it
    runs on. A cadence a set cycle has not been written yet, the weeks straight after a rotation, drops off
    the line rather than promising pods no day carries."""
    weekends: dict[str, set[bool]] = {}
    for day in days:
        if day < today:
            continue
        for code in extras_on(day):
            role_name = fi.CUBE_ROLE_NAME if is_custom(code) else fi.FLASHBACK_ROLE_NAME
            weekends.setdefault(role_name, set()).add(is_weekend(day))
    ordered = ((fi.FLASHBACK_ROLE_NAME, fi.flashback_emoji()), (fi.CUBE_ROLE_NAME, fi.cube_emoji()))
    return [
        (role_name, symbol, _days_label(weekends[role_name]))
        for role_name, symbol in ordered if role_name in weekends
    ]


def _days_label(weekends: set[bool]) -> str:
    """Named as the week half a role's pods sit in, and left unnamed once they sit in both, so the label
    never promises a day the table does not carry."""
    if weekends == {False}:
        return DAYS_WEEKDAY
    if weekends == {True}:
        return DAYS_WEEKEND
    return ""


def championship_line(guild: discord.Guild | None, days: list[date], now: datetime,
                      championship_at: datetime | None) -> str:
    """Empty in a span with no championship in it. A played one keeps its calendar crown but leaves this
    line, where its relative timestamp would read as upcoming."""
    if championship_at is None or championship_at <= now or championship_at.date() not in days:
        return ""
    return MSG_CHAMPIONSHIP.format(
        role=role_mention(guild, SET_CHAMPION_ROLE_NAME), unix=int(championship_at.timestamp()),
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PodSchedule(bot))
