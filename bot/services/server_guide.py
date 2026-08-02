"""Rendering for the bot-managed Server Guide pages, sourced from bot/server_guide/*.md.

Pure text/spec logic — no Discord I/O, which lives in bot/commands/guide.py. Each page is one
markdown file whose first line is the embed title, optionally followed by a ``Topic:`` line that
becomes the channel description; several pages can share one channel (the website channel carries a
website embed and a bot embed). The body carries placeholders resolved per guild at render time:
``{#channel-name}`` → channel mention by emoji-stripped name, ``{latest-set-channel}`` → the
newest-created channel in MTG Strategy (the link rotates early, the moment a mod creates the next
set's channel), ``{site}`` → the public site URL, ``{bot}`` → the bot's mention, ``{feedback}`` →
the feedback channel mention, ``{moderator}`` → the Moderator role mention (sent with pings
suppressed, so it links without notifying), ``:name:`` → the matching application emoji (unknown names stay
literal, marking what still needs an upload). A page may also carry ``Topic:`` and ``Thumbnail:``
directive lines right under the title. Editing a page is editing its markdown and re-running
`!guide` — the bot being the only Discord-side editor is what keeps the guide updatable at all,
since native Server Guide resource channels stop being normal channels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bot import emojis
from bot.commands import descriptions
from bot.config import settings
from bot.services.format_schedule import LATEST_SET_CATEGORY, latest_channel_in_category

SOURCE_DIR = Path(__file__).resolve().parents[1] / "server_guide"


@dataclass(frozen=True)
class GuidePage:
    name: str
    channel: str


@dataclass(frozen=True)
class GuideContent:
    title: str
    body: str
    topic: str | None = None
    thumbnail: str | None = None


GUIDE_PAGES: tuple[GuidePage, ...] = (
    GuidePage("channel-overview", "channel-overview"),
    GuidePage("rules", "rules"),
    GuidePage("dischord-bot", "bot-guide"),
)
OVERVIEW_PAGE = GUIDE_PAGES[0]


def pages_by_channel() -> list[tuple[str, tuple[GuidePage, ...]]]:
    """GUIDE_PAGES grouped by their target channel, in first-appearance order, so a channel that
    carries several pages is synced as one unit."""
    grouped: dict[str, list[GuidePage]] = {}
    for page in GUIDE_PAGES:
        grouped.setdefault(page.channel, []).append(page)
    return [(channel, tuple(pages)) for channel, pages in grouped.items()]

CHANNEL_PLACEHOLDER = re.compile(r"\{#([a-z0-9-]+)\}")
EMOJI_PLACEHOLDER = re.compile(r":([A-Za-z0-9_]+):")
DESC_PLACEHOLDER = re.compile(r"\{desc:([A-Z_]+)\}")
DIRECTIVE_LINE = re.compile(r"^(Topic|Thumbnail):\s*(.*)$")
MISSING_SET_CHANNEL = "the newest set's channel"


def stripped_channel_name(name: str) -> str:
    """Channel name without its emoji prefix — "🧭-channel-overview" → "channel-overview"."""
    return re.sub(r"^[^a-z0-9]+", "", name.lower())


def find_channel(text_channels, name: str):
    """Exact match on the emoji-stripped name first, then substring — so "rules" resolves to
    #📏-rules rather than #⚖-mtg-rules-questions."""
    for channel in text_channels:
        if stripped_channel_name(channel.name) == name:
            return channel
    for channel in text_channels:
        if name in channel.name:
            return channel
    return None


def parse_page(name: str) -> GuideContent:
    """A page's raw content — the source's leading `# Title` line, any `Topic:`/`Thumbnail:`
    directive lines right under it, and everything after as the body."""
    text = (SOURCE_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    first_line, _, body = text.partition("\n")
    title = first_line.lstrip("#").strip()
    body = body.strip()
    directives: dict[str, str] = {}
    while True:
        first_line, _, rest = body.partition("\n")
        match = DIRECTIVE_LINE.match(first_line)
        if match is None:
            break
        directives[match.group(1)] = match.group(2).strip()
        body = rest.strip()
    return GuideContent(title=title, body=body,
                        topic=directives.get("Topic"), thumbnail=directives.get("Thumbnail"))


def render_page(name: str, text_channels, bot_mention: str = "",
                mod_mention: str = "@Moderator", pod_drafters_mention: str = "@Pod Drafters") -> GuideContent:
    content = parse_page(name)
    site = settings.public_site_url.rstrip("/")
    topic = content.topic.replace("{site}", site) if content.topic is not None else None
    thumbnail = content.thumbnail.replace("{site}", site) if content.thumbnail is not None else None
    body = (content.body.replace("{site}", site).replace("{bot}", bot_mention)
            .replace("{feedback}", f"<#{settings.feedback_channel_id}>")
            .replace("{moderator}", mod_mention)
            .replace("{pod-drafters}", pod_drafters_mention))
    body = EMOJI_PLACEHOLDER.sub(lambda match: emojis.get(match.group(1)) or match.group(0), body)
    body = DESC_PLACEHOLDER.sub(lambda match: getattr(descriptions, match.group(1), match.group(0)), body)
    if "{latest-set-channel}" in body:
        set_channel = latest_channel_in_category(text_channels, LATEST_SET_CATEGORY)
        mention = set_channel.mention if set_channel is not None else MISSING_SET_CHANNEL
        body = body.replace("{latest-set-channel}", mention)

    def resolve(match: re.Match) -> str:
        channel = find_channel(text_channels, match.group(1))
        return channel.mention if channel is not None else f"#{match.group(1)}"

    return GuideContent(title=content.title, body=CHANNEL_PLACEHOLDER.sub(resolve, body),
                        topic=topic, thumbnail=thumbnail)
