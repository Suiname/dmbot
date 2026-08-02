"""📚 Save Resource — copy a post into the server's resources forum as its own thread.

Anyone can save any post. The save modal lets the saver rename the thread, edit the copied body,
apply forum tags, and consolidate nearby posts by the same author into the one thread. Text and
attachments are re-posted with a jump link back to the source. Threads are created through a
bot-managed webhook wearing the original author's name and avatar so the forum listing reads like
the author posted it; without Manage Webhooks the bot posts as itself with explicit attribution.
The bot marks the saved post with a 📚 reaction. Saving a marked post again opens the modal in
edit mode against the existing thread when it can be located (the source jump link is searched in
recent threads' starter messages); otherwise it warns before allowing a second thread. Guild-only:
the target is the first forum channel whose name contains "-resources".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands, ui
from discord.ext import commands

from bot import audit
from bot.config import settings
from bot.services import bot_log

logger = logging.getLogger(__name__)

RESOURCE_CHANNEL_MATCH = "-resources"
SAVED_MARKER = "📚"
WEBHOOK_NAME = f"{settings.community_name} Resources"
WEBHOOK_USERNAME_LIMIT = 80
THREAD_NAME_LIMIT = 100
MESSAGE_CONTENT_LIMIT = 2000
BODY_EDIT_LIMIT = 4000
MESSAGE_FILE_LIMIT = 10
FORUM_TAG_LIMIT = 5
SELECT_OPTION_LIMIT = 25
CANDIDATE_SCAN_LIMIT = 25
EXISTING_SCAN_LIMIT = 10

MSG_NO_FORUM = "No resources channel found in this server — expected a forum channel with `-resources` in its name."
MSG_ALREADY_SAVED = "This post was already saved to {channel}. Save it again anyway?"
MSG_SAVED = "📚 Saved [this post]({post}) to {thread}."
MSG_UPDATED = "📚 Updated {thread} from [this post]({post})."


@app_commands.context_menu(name="📚 Save Resource")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.allowed_installs(guilds=True, users=False)
async def save_resource_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    audit.event("save_resource_invoked", user_id=str(interaction.user.id), message_id=str(message.id))
    forum = find_resources_forum(interaction.guild)
    if forum is None:
        await interaction.response.send_message(MSG_NO_FORUM, ephemeral=True)
        return
    if _already_saved(message):
        existing, starter = await _find_existing_resource(forum, message)
        if existing is not None:
            candidates = await _consolidation_candidates(message)
            modal = _SaveResourceModal(message, forum, candidates, existing=existing, starter=starter)
            await interaction.response.send_modal(modal)
            return
        await interaction.response.send_message(
            MSG_ALREADY_SAVED.format(channel=forum.mention),
            view=_SaveAnywayView(message, forum, str(interaction.user.id)),
            ephemeral=True,
        )
        return
    candidates = await _consolidation_candidates(message)
    await interaction.response.send_modal(_SaveResourceModal(message, forum, candidates))


def default_thread_name(content: str | None, author_name: str) -> str:
    for line in (content or "").splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return cleaned[:THREAD_NAME_LIMIT]
    return f"Resource from {author_name}"[:THREAD_NAME_LIMIT]


def split_content(text: str, limit: int = MESSAGE_CONTENT_LIMIT) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def editable_body_from_starter(content: str) -> str:
    lines = []
    for line in content.splitlines():
        if line.startswith("-# Original post ") or line.startswith(f"{SAVED_MARKER} Saved by "):
            continue
        lines.append(line)
    return "\n".join(lines).strip()[:BODY_EDIT_LIMIT]


def merge_texts(entries: list[tuple[datetime, str | None]]) -> list[str]:
    ordered = sorted(entries, key=lambda entry: entry[0])
    texts: list[str] = []
    for _, text in ordered:
        cleaned = (text or "").strip()
        if cleaned:
            texts.append(cleaned)
    return texts


def find_resources_forum(guild: discord.Guild | None) -> discord.ForumChannel | None:
    if guild is None:
        return None
    for channel in guild.forums:
        if RESOURCE_CHANNEL_MATCH in channel.name:
            return channel
    return None


class _SaveResourceModal(ui.Modal, title="Save Resource"):
    def __init__(
        self,
        message: discord.Message,
        forum: discord.ForumChannel,
        candidates: list[discord.Message],
        existing: discord.Thread | None = None,
        starter: discord.Message | None = None,
    ) -> None:
        super().__init__()
        if existing is not None:
            self.title = "Update Resource"
        self._message = message
        self._forum = forum
        self._existing = existing
        self._starter = starter
        self._candidates = {str(candidate.id): candidate for candidate in candidates}
        name_default = existing.name if existing else default_thread_name(
            message.content, message.author.display_name
        )
        self.thread_name = ui.TextInput(max_length=THREAD_NAME_LIMIT, required=True, default=name_default)
        self.add_item(ui.Label(text="Thread name", component=self.thread_name))
        if starter is not None:
            body_default = editable_body_from_starter(starter.content)
        else:
            body_default = (message.content or "")[:BODY_EDIT_LIMIT]
        self.body = ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=BODY_EDIT_LIMIT, required=False, default=body_default
        )
        self.add_item(ui.Label(text="Body", component=self.body))
        applied_tag_ids = {tag.id for tag in existing.applied_tags} if existing else set()
        self._tag_select = _build_tag_select(forum, applied_tag_ids)
        if self._tag_select is not None:
            self.add_item(ui.Label(text="Tags", component=self._tag_select))
        self._post_select = _build_post_select(candidates, message.author.display_name)
        if self._post_select is not None:
            label = ui.Label(
                text="Consolidate more posts",
                description="Nearby posts by the same author to fold into this resource",
                component=self._post_select,
            )
            self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        name = self.thread_name.value.strip() or default_thread_name(None, self._message.author.display_name)
        extras = self._selected_extras()
        tags = self._selected_tags()
        if self._existing is not None and self._starter is not None:
            thread = await _update_resource_thread(
                self._forum, self._existing, self._starter, self._message, extras, self.body.value,
                name, tags, interaction.user,
            )
            verb, notice = "updated", MSG_UPDATED
        else:
            thread = await _create_resource_thread(
                self._forum, self._message, extras, self.body.value, name, tags, interaction.user
            )
            await _mark_saved(self._message)
            verb, notice = "saved", MSG_SAVED
        audit.event(
            f"resource_{verb}", user_id=str(interaction.user.id), message_id=str(self._message.id),
            thread_id=str(thread.id), extra_posts=len(extras), tags=[tag.name for tag in tags],
        )
        logger.info(
            f"save-resource: {interaction.user.id} {verb} message {self._message.id} "
            f"(+{len(extras)} consolidated) to thread {thread.id}"
        )
        await bot_log.get(interaction.client).post_plain(
            f"📚 **{interaction.user.display_name}** {verb} [a post]({self._message.jump_url}) by "
            f"**{self._message.author.display_name}** to {thread.mention}"
        )
        await interaction.followup.send(notice.format(post=self._message.jump_url, thread=thread.mention))

    def _selected_extras(self) -> list[discord.Message]:
        if self._post_select is None:
            return []
        return [self._candidates[value] for value in self._post_select.values if value in self._candidates]

    def _selected_tags(self) -> list[discord.ForumTag]:
        if self._tag_select is None:
            return []
        tags: list[discord.ForumTag] = []
        for value in self._tag_select.values:
            tag = self._forum.get_tag(int(value))
            if tag is not None:
                tags.append(tag)
        return tags


class _SaveAnywayView(ui.View):
    def __init__(self, message: discord.Message, forum: discord.ForumChannel, user_id: str) -> None:
        super().__init__(timeout=300)
        self._message = message
        self._forum = forum
        self._user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self._user_id

    @ui.button(label="Save Anyway", style=discord.ButtonStyle.primary, emoji="📚")
    async def save_anyway(self, interaction: discord.Interaction, button: ui.Button) -> None:
        candidates = await _consolidation_candidates(self._message)
        await interaction.response.send_modal(_SaveResourceModal(self._message, self._forum, candidates))

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.edit_message(content="Canceled.", view=None)


def _build_tag_select(forum: discord.ForumChannel, selected_ids: set[int]) -> ui.Select | None:
    if not forum.available_tags:
        return None
    options = [
        discord.SelectOption(label=tag.name, value=str(tag.id), emoji=tag.emoji, default=(tag.id in selected_ids))
        for tag in forum.available_tags[:SELECT_OPTION_LIMIT]
    ]
    return ui.Select(
        placeholder="Tags", options=options,
        min_values=1, max_values=min(FORUM_TAG_LIMIT, len(options)), required=True,
    )


def _build_post_select(candidates: list[discord.Message], author_name: str) -> ui.Select | None:
    if not candidates:
        return None
    options = [
        discord.SelectOption(
            label=_candidate_label(candidate), value=str(candidate.id),
            description=candidate.created_at.strftime("%b %d %H:%M UTC"),
        )
        for candidate in candidates
    ]
    return ui.Select(
        placeholder=f"More posts by {author_name}"[:100], options=options,
        min_values=0, max_values=len(options), required=False,
    )


def _candidate_label(message: discord.Message) -> str:
    if message.content and message.content.strip():
        return default_thread_name(message.content, message.author.display_name)
    return f"📎 {len(message.attachments)} attachment(s)"


async def _consolidation_candidates(message: discord.Message) -> list[discord.Message]:
    try:
        nearby = [m async for m in message.channel.history(limit=CANDIDATE_SCAN_LIMIT, after=message)]
    except discord.HTTPException:
        logger.warning(f"save-resource: could not scan after post {message.id} for consolidation candidates")
        return []
    posts = [m for m in nearby if m.author.id == message.author.id and (m.content or m.attachments)]
    posts.sort(key=lambda m: m.created_at)
    return posts[:SELECT_OPTION_LIMIT]


async def _create_resource_thread(
    forum: discord.ForumChannel,
    primary: discord.Message,
    extras: list[discord.Message],
    edited_body: str,
    name: str,
    tags: list[discord.ForumTag],
    saver: discord.abc.User,
) -> discord.Thread:
    webhook = await _resource_webhook(forum)
    if webhook is not None:
        try:
            return await _create_impersonated_thread(webhook, forum, primary, extras, edited_body, name, tags, saver)
        except discord.HTTPException:
            logger.warning(f"save-resource: webhook thread failed for message {primary.id}, posting as the bot")
    return await _create_bot_thread(forum, primary, extras, edited_body, name, tags, saver)


async def _create_impersonated_thread(
    webhook: discord.Webhook,
    forum: discord.ForumChannel,
    primary: discord.Message,
    extras: list[discord.Message],
    edited_body: str,
    name: str,
    tags: list[discord.ForumTag],
    saver: discord.abc.User,
) -> discord.Thread:
    author = primary.author
    chunks, attachments = _assemble_chunks(primary, extras, edited_body, footer=_impersonated_footer(primary, saver))
    files = await _attachment_files(attachments)
    author_ping_only = discord.AllowedMentions(everyone=False, roles=False, users=[author], replied_user=False)
    identity = {
        "username": author.display_name[:WEBHOOK_USERNAME_LIMIT],
        "avatar_url": author.display_avatar.url,
        "allowed_mentions": author_ping_only,
        "wait": True,
    }
    try:
        starter = await webhook.send(content=chunks[0], files=files, thread_name=name, applied_tags=tags, **identity)
    except discord.HTTPException:
        logger.warning(f"save-resource: file re-upload failed for message {primary.id}, linking attachments instead")
        urls = "\n".join(attachment.url for attachment in attachments)
        chunks = split_content(f"{chunks[0]}\n\n{urls}" if urls else chunks[0]) + chunks[1:]
        starter = await webhook.send(content=chunks[0], thread_name=name, applied_tags=tags, **identity)
    thread = forum.get_thread(starter.id) or await forum.guild.fetch_channel(starter.id)
    for chunk in chunks[1:]:
        try:
            await webhook.send(content=chunk, thread=thread, **identity)
        except discord.HTTPException:
            logger.warning(f"save-resource: could not post an overflow chunk in thread {thread.id}", exc_info=True)
    return thread


async def _create_bot_thread(
    forum: discord.ForumChannel,
    primary: discord.Message,
    extras: list[discord.Message],
    edited_body: str,
    name: str,
    tags: list[discord.ForumTag],
    saver: discord.abc.User,
) -> discord.Thread:
    chunks, attachments = _assemble_chunks(primary, extras, edited_body, header=_bot_header(primary, saver))
    files = await _attachment_files(attachments)
    no_pings = discord.AllowedMentions.none()
    try:
        created = await forum.create_thread(
            name=name, content=chunks[0], files=files, applied_tags=tags, allowed_mentions=no_pings
        )
    except discord.HTTPException:
        logger.warning(f"save-resource: file re-upload failed for message {primary.id}, linking attachments instead")
        urls = "\n".join(attachment.url for attachment in attachments)
        chunks = split_content(f"{chunks[0]}\n\n{urls}" if urls else chunks[0]) + chunks[1:]
        created = await forum.create_thread(
            name=name, content=chunks[0], applied_tags=tags, allowed_mentions=no_pings
        )
    for chunk in chunks[1:]:
        await created.thread.send(chunk, allowed_mentions=no_pings)
    return created.thread


async def _update_resource_thread(
    forum: discord.ForumChannel,
    thread: discord.Thread,
    starter: discord.Message,
    primary: discord.Message,
    extras: list[discord.Message],
    edited_body: str,
    name: str,
    tags: list[discord.ForumTag],
    saver: discord.abc.User,
) -> discord.Thread:
    """Existing attachments stay on the starter; consolidated posts' attachments are linked as URLs
    because message edits cannot add uploads through a webhook identity swap."""
    no_pings = discord.AllowedMentions.none()
    if starter.webhook_id is not None:
        footer = _impersonated_footer(primary, saver)
        chunks, _ = _assemble_chunks(primary, extras, edited_body, footer=footer, upload_files=False)
        webhook = await _resource_webhook(forum)
        if webhook is None or webhook.id != starter.webhook_id:
            logger.warning(f"save-resource: webhook for thread {thread.id} is gone, only name/tags updated")
            chunks = []
        else:
            await webhook.edit_message(starter.id, content=chunks[0], thread=thread, allowed_mentions=no_pings)
    else:
        header = _bot_header(primary, saver)
        chunks, _ = _assemble_chunks(primary, extras, edited_body, header=header, upload_files=False)
        await starter.edit(content=chunks[0], allowed_mentions=no_pings)
    await thread.edit(name=name, applied_tags=tags)
    for chunk in chunks[1:]:
        await thread.send(chunk, allowed_mentions=no_pings)
    return thread


async def _find_existing_resource(
    forum: discord.ForumChannel, message: discord.Message
) -> tuple[discord.Thread | None, discord.Message | None]:
    recent = sorted(forum.threads, key=lambda thread: thread.id, reverse=True)[:EXISTING_SCAN_LIMIT]
    starters = await asyncio.gather(*(_starter_message(thread) for thread in recent))
    for thread, starter in zip(recent, starters):
        if starter is not None and message.jump_url in starter.content:
            return thread, starter
    return None, None


async def _starter_message(thread: discord.Thread) -> discord.Message | None:
    if thread.starter_message is not None:
        return thread.starter_message
    try:
        return await thread.fetch_message(thread.id)
    except discord.HTTPException:
        return None


def _impersonated_footer(primary: discord.Message, saver: discord.abc.User) -> str:
    footer = f"-# Original post {primary.jump_url} by {primary.author.mention}"
    if saver.id != primary.author.id:
        footer += f", saved by {saver.mention}"
    return footer


def _bot_header(primary: discord.Message, saver: discord.abc.User) -> str:
    return f"{SAVED_MARKER} Saved by {saver.mention} from {primary.author.mention}'s [post]({primary.jump_url})"


def _assemble_chunks(
    primary: discord.Message,
    extras: list[discord.Message],
    edited_body: str,
    header: str | None = None,
    footer: str | None = None,
    upload_files: bool = True,
) -> tuple[list[str], list[discord.Attachment]]:
    entries = [(primary.created_at, edited_body)]
    entries.extend((extra.created_at, extra.content) for extra in extras)
    texts = merge_texts(entries)
    if upload_files:
        ordered_sources = sorted((primary, *extras), key=lambda m: m.created_at)
        attachments = [attachment for source in ordered_sources for attachment in source.attachments]
        uploads = attachments[:MESSAGE_FILE_LIMIT]
        link_urls = [attachment.url for attachment in attachments[MESSAGE_FILE_LIMIT:]]
    else:
        uploads = []
        link_urls = [attachment.url for extra in extras for attachment in extra.attachments]
    parts = [part for part in (header, *texts, *link_urls, footer) if part]
    return split_content("\n\n".join(parts)), uploads


async def _resource_webhook(forum: discord.ForumChannel) -> discord.Webhook | None:
    try:
        webhooks = await forum.webhooks()
    except discord.Forbidden:
        logger.warning(f"save-resource: missing Manage Webhooks in #{forum.name}, threads will post as the bot")
        return None
    for webhook in webhooks:
        if webhook.name == WEBHOOK_NAME and webhook.token:
            return webhook
    try:
        return await forum.create_webhook(name=WEBHOOK_NAME)
    except discord.HTTPException:
        logger.warning(f"save-resource: could not create the webhook in #{forum.name}", exc_info=True)
        return None


async def _attachment_files(attachments: list[discord.Attachment]) -> list[discord.File]:
    files: list[discord.File] = []
    for attachment in attachments:
        try:
            files.append(await attachment.to_file())
        except discord.HTTPException:
            logger.warning(f"save-resource: could not download attachment {attachment.url}")
    return files


def _already_saved(message: discord.Message) -> bool:
    return any(reaction.me and str(reaction.emoji) == SAVED_MARKER for reaction in message.reactions)


async def _mark_saved(message: discord.Message) -> None:
    try:
        await message.add_reaction(SAVED_MARKER)
    except discord.HTTPException:
        logger.warning(f"save-resource: could not react to post {message.id}", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(save_resource_menu)
