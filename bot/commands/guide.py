"""`!guide` — sync the Server Guide channels from bot/server_guide/*.md.

Available to the bot owner, administrators and the Moderator role. Each page renders into a
Components V2 container: the title sits beside the page thumbnail, the rest of the body runs
full-width below so lists never wrap around the image. Pages are synced per channel — a channel
whose containers all match the source is left alone, one holding a message per page is edited in
place so the sync leaves no unread mark, and anything else has every guide message deleted and
reposted in page order, so a multi-page channel never ends up out of order. Pages post through a
bot-managed webhook wearing the server owner's name and avatar, so the guide reads as posted by them
(with Discord's APP tag); without Manage Webhooks the bot posts as itself. Native Server Guide
resource channels default-deny the bot — each needs explicit channel-level overwrites for the bot's
role (View Channel, Send Messages, Read Message History, Embed Links); server-level role grants lose
to the channels' @everyone deny.
"""
from __future__ import annotations

import logging

import discord
from discord import ui
from discord.ext import commands
from discord.http import Route

from bot import audit
from bot.commands.authorization import MODERATOR_ROLE_NAME, moderator_authorized
from bot.commands.pod_guide import GUIDE_MARKER, render_pod_guide_embed_body
from bot.config import settings
from bot.services.format_schedule import (
    LATEST_SET_CATEGORY,
    active_set_seed,
    channel_for_set,
    set_tracking_todo_index,
)
from bot.services.pod_schedule import POD_DRAFTERS_ROLE_NAME
from bot.services.server_guide import GuideContent, GuidePage, find_channel, pages_by_channel, render_page

log = logging.getLogger(__name__)

GUIDE_COLOR = discord.Color.green()
HISTORY_SCAN_LIMIT = 20
WEBHOOK_NAME = f"{settings.community_name} Server Guide"
WEBHOOK_USERNAME_LIMIT = 80
WEBHOOK_FALLBACK_NOTE = " (posted as the bot, grant Manage Webhooks to post as the server owner)"

TEXT_DISPLAY_TYPE = 10

SYNC_CURRENT = "current"
SYNC_UPDATED = "updated"
SYNC_POSTED = "posted"
SYNC_NO_CHANNEL = "no-channel"
SYNC_FORBIDDEN = "forbidden"
SYNC_FAILED = "failed"
SYNC_STALE = "stale"


async def sync_channel(guild: discord.Guild, channel_name: str,
                       pages: tuple[GuidePage, ...]) -> tuple[str, str]:
    """Reconcile every guide page bound to one channel: (status, human-readable result line)."""
    channel = find_channel(guild.text_channels, channel_name)
    if channel is None:
        return SYNC_NO_CHANNEL, f"⚠️ `{channel_name}`: no matching channel"
    mod_mention = _moderator_mention(guild)
    pod_drafters_mention = _pod_drafters_mention(guild)
    rendered = [render_page(page.name, guild.text_channels, _bot_mention(guild), mod_mention, pod_drafters_mention)
                for page in pages]
    show_titles = len(pages) > 1
    views = [_build_view(content, show_title=show_titles) for content in rendered]
    try:
        webhook = await _guide_webhook(channel)
        webhook_note = "" if webhook is not None else WEBHOOK_FALLBACK_NOTE
        messages = await _guide_messages(channel, guild.me, webhook)
        topic_note = await _sync_topic(channel, _channel_topic(rendered))
        if _all_current(messages, views, webhook):
            return SYNC_CURRENT, f"✅ {channel.mention} up to date{topic_note}{webhook_note}"
        if _editable_in_place(messages, views, webhook):
            await _edit_pages(webhook, messages, views)
            return SYNC_UPDATED, f"✅ {channel.mention} edited{topic_note}{webhook_note}"
        had_existing = bool(messages)
        for message in messages:
            await _delete_guide_message(message, webhook)
        await _post_pages(channel, webhook, views)
        status = SYNC_UPDATED if had_existing else SYNC_POSTED
        word = "reposted" if had_existing else "posted"
        return status, f"✅ {channel.mention} {word}{topic_note}{webhook_note}"
    except discord.Forbidden:
        return SYNC_FORBIDDEN, (f"⚠️ {channel.mention}: missing permissions "
                                "(View Channel, Send Messages, Read Message History, Embed Links)")
    except discord.HTTPException:
        log.warning(f"guide: sync failed for #{channel.name}", exc_info=True)
        return SYNC_FAILED, f"⚠️ {channel.mention}: sync failed, see logs"


async def sync_set_tracking_todo(guild: discord.Guild, http) -> tuple[str, str]:
    """Keep the 'See what people are discussing' Server Guide To-Do pointed at the active set's channel:
    (status, human-readable result line). Discord blocks bots from editing the Server Guide today, so a
    drift is reported for a mod to fix by hand in Server Settings → Onboarding — the write is still
    attempted first, so the To-Do self-heals if that restriction is ever lifted."""
    route = Route("GET", "/guilds/{guild_id}/new-member-welcome", guild_id=guild.id)
    try:
        welcome = await http.request(route)
    except discord.HTTPException:
        return SYNC_FAILED, "⚠️ Latest channel: could not read the Server Guide"
    if not isinstance(welcome, dict):
        return SYNC_NO_CHANNEL, "⚠️ Latest channel: Server Guide not configured on this server"
    actions = welcome.get("new_member_actions") or []
    index = set_tracking_todo_index(actions, guild.channels)
    if index is None:
        return SYNC_NO_CHANNEL, "⚠️ Latest channel: no set-tracking To-Do found"
    seed = active_set_seed()
    target = channel_for_set(guild.channels, seed, LATEST_SET_CATEGORY)
    if target is None:
        return SYNC_NO_CHANNEL, f"⚠️ Latest channel: no channel found for the active set {seed.code}"
    current_id = str(actions[index].get("channel_id"))
    if current_id == str(target.id):
        return SYNC_CURRENT, f"✅ Latest channel points to {target.mention}"
    if await _repoint_set_tracking_todo(guild, http, welcome, actions, index, target):
        return SYNC_UPDATED, f"🔄 Latest channel → {target.mention}"
    current = guild.get_channel(int(current_id)) if current_id.isdigit() else None
    current_ref = current.mention if current is not None else f"`{current_id}`"
    return SYNC_STALE, (f"⚠️ Latest channel still points to {current_ref} but should be {target.mention}\n"
                        'Update the "See what people are discussing" To-Do under '
                        "**Server Settings → Onboarding → Server Guide**.")


async def _repoint_set_tracking_todo(guild: discord.Guild, http, welcome, actions, index, target) -> bool:
    """Write the To-Do's new channel, ``True`` on success. Discord blocks bots from the Server Guide
    endpoint today so this normally fails; kept so a rotation heals the To-Do on its own if that lifts."""
    updated = [dict(action) for action in actions]
    updated[index]["channel_id"] = str(target.id)
    body = {"enabled": welcome["enabled"], "welcome_message": welcome["welcome_message"],
            "new_member_actions": updated}
    try:
        await http.request(Route("PUT", "/guilds/{guild_id}/new-member-welcome", guild_id=guild.id), json=body)
        return True
    except discord.HTTPException:
        return False


async def sync_pod_guide(guild: discord.Guild) -> tuple[str, str]:
    """Reconcile the pinned Pod Draft guide in the pod-draft coordination channel
    (settings.pod_draft_channel_id, resolved by id so a rename doesn't matter) by editing the existing
    message in place — so the now-quiet channel never gets a fresh post — and clearing any stale
    duplicate pins: (status, human-readable result line)."""
    channel = guild.get_channel(settings.pod_draft_channel_id)
    if channel is None:
        return SYNC_NO_CHANNEL, "⚠️ Pod guide: pod-draft coordination channel not found"
    body = render_pod_guide_embed_body(_pod_drafters_mention(guild))
    embed = discord.Embed(description=body, color=GUIDE_COLOR)
    try:
        existing = await _pinned_pod_guides(channel, guild.me)
        if not existing:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await message.pin(reason="pod draft guide")
            return SYNC_POSTED, f"✅ {channel.mention} pod guide posted"
        primary, duplicates = existing[0], existing[1:]
        changed = bool(duplicates)
        for stale in duplicates:
            await _delete_guide_message(stale, None)
        if _embed_body(primary) != body:
            await primary.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            changed = True
        word = "updated" if changed else "up to date"
        return (SYNC_UPDATED if changed else SYNC_CURRENT), f"✅ {channel.mention} pod guide {word}"
    except discord.Forbidden:
        return SYNC_FORBIDDEN, (f"⚠️ {channel.mention}: pod guide needs "
                                "View Channel, Send Messages, Read Message History, Embed Links")
    except discord.HTTPException:
        log.warning("guide: pod guide sync failed", exc_info=True)
        return SYNC_FAILED, f"⚠️ {channel.mention}: pod guide sync failed, see logs"


async def _pinned_pod_guides(channel: discord.TextChannel, me: discord.Member) -> list[discord.Message]:
    """Pinned messages this bot posted that carry the pod guide, oldest-first so the first is the
    canonical one and the rest are stale duplicates to drop."""
    pins = await channel.pins()
    guides = [
        message for message in pins
        if message.author.id == me.id
        and any(GUIDE_MARKER in (embed.description or "") for embed in message.embeds)
    ]
    guides.reverse()
    return guides


def _embed_body(message: discord.Message) -> str | None:
    for embed in message.embeds:
        if embed.description:
            return embed.description
    return None


def _build_view(content: GuideContent, show_title: bool = True) -> ui.LayoutView:
    """Render a page as a green container. With a thumbnail, the title and leading paragraph sit
    beside it and the rest of the body runs full-width below so lists never wrap around the image;
    without one, the whole page is a single full-width block. Blank lines stay as paragraph spacing
    rather than becoming dividers, since the sources use them inconsistently. `show_title` is false
    for a channel's sole page, whose title just repeats the channel name Discord already shows."""
    view = ui.LayoutView(timeout=None)
    container = ui.Container(accent_colour=GUIDE_COLOR)
    if content.thumbnail is not None:
        lead, _, remainder = content.body.partition("\n\n")
        header = f"## {content.title}\n{lead}".rstrip() if show_title else lead.strip()
        container.add_item(ui.Section(ui.TextDisplay(header), accessory=ui.Thumbnail(media=content.thumbnail)))
        if remainder.strip():
            container.add_item(ui.TextDisplay(remainder.strip()))
    else:
        body = f"## {content.title}\n\n{content.body}" if show_title else content.body
        container.add_item(ui.TextDisplay(body.strip()))
    view.add_item(container)
    return view


def _channel_topic(rendered: list[GuideContent]) -> str | None:
    for content in rendered:
        if content.topic is not None:
            return content.topic
    return None


def _all_current(messages: list[discord.Message], views: list[ui.LayoutView],
                 webhook: discord.Webhook | None) -> bool:
    """A channel is current when its guide messages match the rendered pages one-for-one in order —
    same count, same author, same content. Matching by position rather than title lets a page carry
    no heading at all (a channel's sole page repeats the channel name, so it drops the title)."""
    if not _editable_in_place(messages, views, webhook):
        return False
    for message, view in zip(messages, views):
        if _message_signature(message) != _view_signature(view):
            return False
    return True


def _editable_in_place(messages: list[discord.Message], views: list[ui.LayoutView],
                       webhook: discord.Webhook | None) -> bool:
    """True when the channel already holds one message per page, in order, all owned by the identity
    the sync posts as now. Editing keeps the channel from showing as unread, so a delete-and-repost
    is kept for the cases an edit cannot express: a page added or removed, or a channel whose pages
    were posted as the bot before it gained its webhook."""
    if len(messages) != len(views):
        return False
    if webhook is None:
        return True
    for message in messages:
        if message.webhook_id != webhook.id:
            return False
    return True


async def _edit_pages(webhook: discord.Webhook | None, messages: list[discord.Message],
                      views: list[ui.LayoutView]) -> None:
    """Rewrite only the pages whose content moved, so a sync that touches one page of a multi-page
    channel leaves the others alone."""
    for message, view in zip(messages, views):
        if _message_signature(message) == _view_signature(view):
            continue
        if webhook is not None:
            await webhook.edit_message(message.id, view=view, allowed_mentions=discord.AllowedMentions.none())
        else:
            await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())


async def _sync_topic(channel: discord.TextChannel, topic: str | None) -> str:
    """Align the channel description with the page's Topic line; a page without one leaves the
    channel alone. Topic edits are their own permission (Manage Channels) and heavily rate-limited,
    so this edits only on change and never fails the message sync."""
    if topic is None or channel.topic == topic:
        return ""
    try:
        await channel.edit(topic=topic)
        return ", topic updated"
    except discord.Forbidden:
        return ", topic skipped (needs Manage Channels)"
    except discord.HTTPException:
        log.warning(f"guide: could not update the topic of #{channel.name}", exc_info=True)
        return ", topic update failed"


async def _post_pages(channel: discord.TextChannel, webhook: discord.Webhook | None,
                      views: list[ui.LayoutView]) -> None:
    """Post the channel's containers in order, through the webhook wearing the server owner's
    identity when one is available, otherwise as the bot."""
    if webhook is None:
        for view in views:
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        return
    owner = channel.guild.owner or await channel.guild.fetch_member(channel.guild.owner_id)
    for view in views:
        await webhook.send(
            view=view,
            username=owner.display_name[:WEBHOOK_USERNAME_LIMIT],
            avatar_url=owner.display_avatar.url,
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True,
        )


def _bot_mention(guild: discord.Guild) -> str:
    """The bot's managed integration role renders in its role colour, nicer than the plain user
    mention; fall back to the user mention when the role can't be resolved."""
    if guild.self_role is not None:
        return guild.self_role.mention
    return guild.me.mention


def _moderator_mention(guild: discord.Guild) -> str:
    role = discord.utils.get(guild.roles, name=MODERATOR_ROLE_NAME)
    return role.mention if role is not None else f"@{MODERATOR_ROLE_NAME}"


def _pod_drafters_mention(guild: discord.Guild) -> str:
    role = discord.utils.get(guild.roles, name=POD_DRAFTERS_ROLE_NAME)
    return role.mention if role is not None else f"@{POD_DRAFTERS_ROLE_NAME}"


async def _delete_guide_message(message: discord.Message, webhook: discord.Webhook | None) -> None:
    """Deletion is idempotent — a message already gone (a stale id from a prior interrupted sync)
    is success, so a 404 never aborts the channel's repost."""
    try:
        if webhook is not None and message.webhook_id == webhook.id:
            await webhook.delete_message(message.id)
        else:
            await message.delete()
    except discord.NotFound:
        pass


async def _guide_webhook(channel: discord.TextChannel) -> discord.Webhook | None:
    try:
        webhooks = await channel.webhooks()
    except discord.Forbidden:
        return None
    for webhook in webhooks:
        if webhook.name == WEBHOOK_NAME and webhook.token:
            return webhook
    try:
        return await channel.create_webhook(name=WEBHOOK_NAME)
    except discord.HTTPException:
        log.warning(f"guide: could not create the webhook in #{channel.name}; posting as the bot")
        return None


async def _guide_messages(channel: discord.TextChannel, me: discord.Member,
                          webhook: discord.Webhook | None) -> list[discord.Message]:
    """Messages in the channel authored by the bot or its guide webhook — everything the guide owns,
    so a repost clears the channel of its own stale pages first — returned oldest-first, the order
    they were posted, so position matching lines up with page order."""
    webhook_id = webhook.id if webhook is not None else None
    messages: list[discord.Message] = []
    async for message in channel.history(limit=HISTORY_SCAN_LIMIT):
        if message.author.id == me.id or message.webhook_id == webhook_id:
            messages.append(message)
    messages.reverse()
    return messages


def _view_signature(view: ui.LayoutView) -> tuple[str, ...]:
    return tuple(_text_contents(view.to_components()))


def _message_signature(message: discord.Message) -> tuple[str, ...]:
    return tuple(_text_contents([component.to_dict() for component in message.components]))


def _text_contents(payload) -> list[str]:
    """Every TextDisplay content string in a component payload, depth-first, in render order."""
    contents: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if node.get("type") == TEXT_DISPLAY_TYPE:
                contents.append(node.get("content", ""))
            walk(node.get("components", []))
            walk(node.get("accessory"))

    walk(payload)
    return contents


async def setup(bot: commands.Bot) -> None:
    @bot.command(name="guide")
    @commands.check(moderator_authorized)
    async def guide_cmd(ctx: commands.Context) -> None:
        """Sync every Server Guide channel and report per-channel results."""
        guild = bot.get_guild(settings.discord_guild_id) if settings.discord_guild_id else None
        if guild is None:
            await ctx.send("⚠️ Guild unavailable.")
            return
        groups = pages_by_channel()
        results = [await sync_channel(guild, channel, pages) for channel, pages in groups]
        pod_status, pod_line = await sync_pod_guide(guild)
        todo_status, todo_line = await sync_set_tracking_todo(guild, bot.http)
        audit.event("guide_sync", user_id=str(ctx.author.id),
                    results={channel: status for (channel, _), (status, _) in zip(groups, results)},
                    pod_guide=pod_status,
                    set_tracking_todo=todo_status)
        lines = "\n".join(line for _, line in results)
        await ctx.send(f"Synced <id:guide>\n{lines}\n{pod_line}\n{todo_line}")
