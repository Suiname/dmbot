"""The T-1 day nudge for a P0P1 contest: ping everyone holding a P0P1 identity with no picks in it.

Targeting rides on the shared ``Reminder`` role, not DMs: a bot can only message a user who shares a guild
with it, so a DM sweep reaches nobody a role grant misses, and the burst reads to Discord as spam. The role
is emptied after the post but never deleted, since a deleted role renders as ``@deleted-role`` in history.

Identity comes from ``auth.identities``, not ``players`` — most voters signed in on the website and never
joined the leaderboard, so it is the only place their Discord id exists.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from bot.config import settings
from bot.database import SessionLocal
from bot.services.ping_roles import REMINDER_COLOR, REMINDER_ROLE_NAME

log = logging.getLogger(__name__)

NON_VOTERS_SQL = text("""
    with known as (
        select user_id from p0p1_voters
        union
        select distinct user_id from p0p1_entries
    )
    select i.provider_id
    from known k
    join auth.identities i on i.user_id = k.user_id and i.provider = 'discord'
    where not exists (
        select 1 from p0p1_entries e
        where e.user_id = k.user_id and e.set_code = :set_code
    )
""")

LOCAL_NON_VOTERS_SQL = text("select discord_id from players where discord_id is not null")

VOTER_COUNT_SQL = text("select count(distinct user_id) from p0p1_entries where set_code = :set_code")

SOURCE_AUTH = "auth"
SOURCE_PLAYERS = "players"


@dataclass(frozen=True)
class ReminderOutcome:
    """``absent`` are voters not in the guild, whom no notification surface can reach. ``source`` is
    ``SOURCE_PLAYERS`` when the dev fallback supplied the audience, so it is not the real one."""
    targeted: int
    pinged: int
    absent: int
    source: str
    restricted: bool


@dataclass(frozen=True)
class NonVoters:
    discord_ids: list[str]
    source: str


def non_voter_discord_ids_sync(set_code: str) -> NonVoters:
    """Supabase owns ``auth``, so no developer database has it. Falling back to every local player keeps the
    role swap exercisable off production; nothing links a ``players`` row to a P0P1 identity, so that
    audience excludes nobody and is labelled ``SOURCE_PLAYERS`` instead of passing as the real list."""
    with SessionLocal() as session:
        try:
            rows = session.execute(NON_VOTERS_SQL, {"set_code": set_code.upper()}).scalars().all()
            return NonVoters([str(row) for row in rows], SOURCE_AUTH)
        except ProgrammingError:
            session.rollback()
            log.warning(f"p0p1-reminder: no auth schema, falling back to every {SOURCE_PLAYERS} row")
            rows = session.execute(LOCAL_NON_VOTERS_SQL).scalars().all()
            return NonVoters([str(row) for row in rows], SOURCE_PLAYERS)


def voter_count_sync(set_code: str) -> int:
    with SessionLocal() as session:
        return session.execute(VOTER_COUNT_SQL, {"set_code": set_code.upper()}).scalar() or 0


async def ping_non_voters(
    guild: discord.Guild, role: discord.Role, set_code: str, build_post,
    *, restrict_to: discord.Member | None = None,
) -> ReminderOutcome:
    """The non-voter list is read immediately before the grants, so a ballot filed during the run is not
    pinged for being unfilled."""
    await _ensure_member_cache(guild)
    non_voters = await asyncio.to_thread(non_voter_discord_ids_sync, set_code)
    discord_ids = audience(guild, non_voters.discord_ids, restrict_to)
    members = _members_for(guild, discord_ids)

    await _empty_role(role)
    pinged = await _grant_all(role, members)
    await _post_ping(role, build_post)
    await _empty_role(role)

    outcome = ReminderOutcome(
        targeted=len(discord_ids), pinged=pinged, absent=len(discord_ids) - len(members),
        source=non_voters.source, restricted=guild.id != settings.production_guild_id,
    )
    log.info(f"p0p1-reminder: {set_code} pinged {outcome.pinged}/{outcome.targeted} from {outcome.source}, "
             f"{outcome.absent} not in guild")
    log.info(f"p0p1-reminder: {set_code} pinged {', '.join(str(member) for member in members) or 'nobody'}")
    return outcome


def audience(
    guild: discord.Guild, discord_ids: list[str], restrict_to: discord.Member | None,
) -> list[str]:
    """Off the production guild the ping reaches only whoever asked for it, and nobody at all when the
    scheduled tick fires. A test server holds real people who never signed up for a P0P1 nudge, and its
    audience comes from the dev fallback anyway, so a full sweep there pings the wrong crowd."""
    if guild.id == settings.production_guild_id:
        return discord_ids
    if restrict_to is None:
        log.info(f"p0p1-reminder: {guild.name} is not the production guild, pinging nobody")
        return []
    log.info(f"p0p1-reminder: {guild.name} is not the production guild, pinging only {restrict_to}")
    return [str(restrict_to.id)]


async def reminder_role(guild: discord.Guild) -> discord.Role | None:
    """The shared reminder role, not mentionable outside the post itself. Created here for a guild whose
    reconcile has not run yet; `MANAGED_ROLES` owns its name and color afterwards, renaming and recoloring
    one that predates either."""
    role = discord.utils.get(guild.roles, name=REMINDER_ROLE_NAME)
    if role is not None:
        return role
    try:
        role = await guild.create_role(
            name=REMINDER_ROLE_NAME, colour=discord.Colour.from_str(REMINDER_COLOR),
            mentionable=False, reason="p0p1 reminder ping",
        )
    except discord.HTTPException:
        log.warning(f"p0p1-reminder: could not create {REMINDER_ROLE_NAME!r} in {guild.name}", exc_info=True)
        return None
    log.info(f"p0p1-reminder: created {REMINDER_ROLE_NAME!r} in {guild.name}")
    return role


async def _ensure_member_cache(guild: discord.Guild) -> None:
    """Both the target lookup and the strip read the member cache, so an unchunked guild would silently
    report most voters as absent and leave stale holders on the role."""
    if guild.chunked:
        return
    try:
        await guild.chunk()
    except (discord.ClientException, asyncio.TimeoutError):
        log.warning(f"p0p1-reminder: could not chunk {guild.name}, reach will be understated", exc_info=True)


def _members_for(guild: discord.Guild, discord_ids: list[str]) -> list[discord.Member]:
    members = []
    for discord_id in discord_ids:
        if not discord_id.isdigit():
            log.warning(f"p0p1-reminder: skipping non-numeric discord id {discord_id!r}")
            continue
        member = guild.get_member(int(discord_id))
        if member is not None:
            members.append(member)
    return members


async def _post_ping(role: discord.Role, build_post) -> None:
    """Make the role mentionable only for the send. Leaving it mentionable would let any member fire it,
    and the alternative — posting a silent role and relying on Mention Everyone — makes the ping depend on
    a permission the bot has no other reason to hold."""
    try:
        await role.edit(mentionable=True, reason="p0p1 reminder ping")
    except discord.HTTPException:
        log.warning(f"p0p1-reminder: could not make {role.name!r} mentionable", exc_info=True)
    try:
        await build_post(role.mention)
    finally:
        try:
            await role.edit(mentionable=False, reason="p0p1 reminder ping sent")
        except discord.HTTPException:
            log.warning(f"p0p1-reminder: {role.name!r} left mentionable", exc_info=True)


async def _grant_all(role: discord.Role, members: list[discord.Member]) -> int:
    granted = 0
    for member in members:
        try:
            await member.add_roles(role, reason="p0p1 reminder ping")
            granted += 1
        except discord.HTTPException:
            log.warning(f"p0p1-reminder: could not grant {role.name!r} to {member}", exc_info=True)
    return granted


async def _empty_role(role: discord.Role) -> None:
    """Strip every holder. Run before the grants as well as after, so a run that died mid-strip leaves the
    next one a clean role instead of pinging last contest's list."""
    for member in list(role.members):
        try:
            await member.remove_roles(role, reason="p0p1 reminder ping sent")
        except discord.HTTPException:
            log.warning(f"p0p1-reminder: could not strip {role.name!r} from {member}", exc_info=True)
