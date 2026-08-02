"""Sync the podcast (Libsyn RSS) and YouTube channel into the ``episodes`` table.

A video that matches a podcast episode upgrades that episode's row (thumbnail + youtube_id);
unmatched videos become standalone rows. Category and set come from the YouTube playlists a
video belongs to, falling back to title inference for podcast-only entries. Rebuilt on each
run — the feeds stay the source of truth.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.config import settings
from bot.media_sets import EVERGREEN, by_code, resolve_set
from bot.models import Episode
from bot.services.youtube import YouTubeClient, YouTubeVideo

log = logging.getLogger(__name__)

CATEGORIES = ("Set Review", "Draft", "Sealed", "Rankings", "Metagame", "Coaching", "Guest", "Evergreen")

# Per-guid classification seed: a one-time LLM pass over the back catalog plus hand-corrections,
# consulted before the rules so a resync reproduces it. Each value is a category string, or
# {"category"?, "set"?} when an episode also needs a manual set the title can't yield.
_SEED_RAW: dict[str, str | dict[str, str]] = json.loads(
    Path(__file__).with_name("episode_category_seed.json").read_text(encoding="utf-8")
)
_SEED_CATEGORY: dict[str, str] = {}
_SEED_SET: dict[str, str] = {}
for _guid, _value in _SEED_RAW.items():
    if isinstance(_value, str):
        _SEED_CATEGORY[_guid] = _value
    else:
        if "category" in _value:
            _SEED_CATEGORY[_guid] = _value["category"]
        if "set" in _value:
            _SEED_SET[_guid] = _value["set"]

_MATCH_WINDOW_S = 3 * 24 * 60 * 60
_DURATION_MATCH_RATIO = 0.1


@dataclass
class SyncResult:
    total: int
    matched: int
    videos_only: int
    podcasts_only: int
    with_set: int


@dataclass
class _Item:
    guid: str
    kind: str
    number: int | None
    title: str
    link: str
    summary: str
    image: str
    published_at: datetime
    duration_seconds: int
    audio_url: str | None
    youtube_id: str | None
    playlists: list[str] = field(default_factory=list)
    category: str = ""
    set_code: str | None = None
    set_name: str | None = None
    set_released_at: date | None = None


def sync_media(session: Session) -> SyncResult:
    podcasts = _fetch_podcast_items(settings.libsyn_feed_url)

    videos: list[YouTubeVideo] = []
    if settings.youtube_api_key:
        client = YouTubeClient(settings.youtube_api_key.get_secret_value(), settings.youtube_channel_handle)
        videos = client.fetch_videos()
    else:
        log.warning("media sync: YOUTUBE_API_KEY unset, syncing podcast feed only")

    items = _merge(podcasts, videos)
    _classify_items(items)
    return _upsert(session, items)


def sync_recent(session: Session, limit: int = 8) -> SyncResult:
    """Insert brand-new drops and backfill a freshly published video onto an existing podcast-only
    row, reclassifying it from the video's playlists. Rows that already carry a video are left
    untouched — the daily ``sync_media`` owns full reconciliation and back-catalogue pruning."""
    podcasts = sorted(
        _fetch_podcast_items(settings.libsyn_feed_url), key=lambda i: i.published_at, reverse=True
    )[:limit]

    videos: list[YouTubeVideo] = []
    if settings.youtube_api_key:
        client = YouTubeClient(settings.youtube_api_key.get_secret_value(), settings.youtube_channel_handle)
        videos = client.fetch_recent_uploads(limit)
    else:
        log.warning("media freshness: YOUTUBE_API_KEY unset, checking podcast feed only")

    items = _merge(podcasts, videos)
    _classify_items(items)
    return _ingest_fresh(session, items)


def _classify_items(items: list[_Item]) -> None:
    for item in items:
        item.category = classify_category(item.playlists, item.title, item.kind, item.guid)
        media_set = resolve_episode_set(item.guid, item.playlists, item.title, item.published_at)
        if media_set is EVERGREEN:
            item.set_code = item.set_name = item.set_released_at = None
        else:
            item.set_code = media_set.code
            item.set_name = media_set.name
            item.set_released_at = media_set.start_date


def classify_category(playlists: list[str], title: str, kind: str, guid: str = "") -> str:
    override = _category_from_title_overrides(title)
    if override:
        return override
    seeded = _SEED_CATEGORY.get(guid)
    category = seeded or _category_from_playlists(playlists) or _category_from_title(title) or _default_category(kind)
    # Draft is YouTube-only draft VODs; a numbered podcast episode is format talk, not a Draft-Along
    if category == "Draft" and kind == "episode":
        return "Metagame"
    return category


def resolve_episode_set(guid: str, playlists: list[str], title: str, published: date | None):
    """Set resolution with the per-guid seed override applied first (for sets the title can't yield)."""
    forced = _SEED_SET.get(guid)
    if forced:
        return by_code(forced)
    return resolve_set(playlists, title, published)


def _default_category(kind: str) -> str:
    return "Draft" if kind == "video" else "Evergreen"


def _merge(podcasts: list[_Item], videos: list[YouTubeVideo]) -> list[_Item]:
    claimed: set[str] = set()
    for podcast in podcasts:
        match = _find_match(podcast, videos, claimed)
        if not match:
            continue
        claimed.add(match.id)
        podcast.image = match.thumbnail or podcast.image
        podcast.youtube_id = match.id
        podcast.playlists = match.playlists

    standalone = [_video_item(v) for v in videos if v.id not in claimed]
    merged = [*podcasts, *standalone]
    merged.sort(key=lambda i: i.published_at, reverse=True)
    return merged


def _find_match(podcast: _Item, videos: list[YouTubeVideo], claimed: set[str]) -> YouTubeVideo | None:
    available = [v for v in videos if v.id not in claimed]

    if podcast.number is not None:
        for video in available:
            if _episode_number(video.title) == podcast.number:
                return video

    podcast_key = _title_key(podcast.title)
    for video in available:
        if _title_key(_clean_title(video.title)) == podcast_key:
            return video

    closest: YouTubeVideo | None = None
    closest_gap = _MATCH_WINDOW_S
    for video in available:
        video_time = _parse_iso(video.published_at)
        if video_time is None:
            continue
        gap = abs((video_time - podcast.published_at).total_seconds())
        if gap <= closest_gap and _title_overlap(podcast.title, video.title):
            closest = video
            closest_gap = gap
    if closest is not None:
        return closest

    return _same_date_match(podcast, available)


def _same_date_match(podcast: _Item, available: list[YouTubeVideo]) -> YouTubeVideo | None:
    """Pair a podcast with the video sharing its publish date when their titles diverge — the audio
    and video cuts of one recording, named differently. Duration-guarded so an unrelated draft VOD
    uploaded the same day isn't absorbed."""
    podcast_date = podcast.published_at.astimezone(timezone.utc).date()
    closest: YouTubeVideo | None = None
    closest_gap = _MATCH_WINDOW_S
    for video in available:
        video_time = _parse_iso(video.published_at)
        if video_time is None:
            continue
        if video_time.astimezone(timezone.utc).date() != podcast_date:
            continue
        if not _durations_match(podcast.duration_seconds, video.duration_seconds):
            continue
        gap = abs((video_time - podcast.published_at).total_seconds())
        if gap <= closest_gap:
            closest = video
            closest_gap = gap
    return closest


def _durations_match(a: int, b: int) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= _DURATION_MATCH_RATIO


def _video_item(video: YouTubeVideo) -> _Item:
    return _Item(
        guid=f"yt:{video.id}",
        kind="video",
        number=_episode_number(video.title),
        title=_clean_title(video.title),
        link=f"https://www.youtube.com/watch?v={video.id}",
        summary=video.description,
        image=video.thumbnail,
        published_at=_parse_iso(video.published_at) or datetime.fromtimestamp(0).astimezone(),
        duration_seconds=video.duration_seconds,
        audio_url=None,
        youtube_id=video.id,
        playlists=video.playlists,
    )


def _upsert(session: Session, items: list[_Item]) -> SyncResult:
    existing = {e.guid: e for e in session.execute(select(Episode)).scalars()}
    seen: set[str] = set()
    for item in items:
        seen.add(item.guid)
        row = existing.get(item.guid)
        if row is None:
            row = Episode(guid=item.guid)
            session.add(row)
        _assign(row, item)

    if len(items) >= 100:
        for guid, row in existing.items():
            if guid not in seen:
                session.delete(row)

    session.commit()
    return _result(items)


def _ingest_fresh(session: Session, items: list[_Item]) -> SyncResult:
    guids = [item.guid for item in items]
    existing = (
        {row.guid: row for row in session.execute(select(Episode).where(Episode.guid.in_(guids))).scalars()}
        if guids
        else {}
    )
    applied: list[_Item] = []
    for item in items:
        row = existing.get(item.guid)
        if row is None:
            row = Episode(guid=item.guid)
            session.add(row)
        else:
            video_just_landed = row.youtube_id is None and item.youtube_id is not None
            if not video_just_landed:
                continue
        _assign(row, item)
        applied.append(item)
    session.commit()
    return _result(applied)


def _assign(row: Episode, item: _Item) -> None:
    row.kind = item.kind
    row.number = item.number
    row.title = item.title
    row.link = item.link
    row.summary = item.summary or None
    row.image = item.image or None
    row.published_at = item.published_at
    row.duration_seconds = item.duration_seconds
    row.audio_url = item.audio_url
    row.youtube_id = item.youtube_id
    row.category = item.category
    row.set_code = item.set_code
    row.set_name = item.set_name
    row.set_released_at = item.set_released_at
    row.playlists = item.playlists or None


def _result(items: list[_Item]) -> SyncResult:
    return SyncResult(
        total=len(items),
        matched=sum(1 for i in items if i.kind == "episode" and i.youtube_id),
        videos_only=sum(1 for i in items if i.kind == "video"),
        podcasts_only=sum(1 for i in items if i.kind == "episode" and not i.youtube_id),
        with_set=sum(1 for i in items if i.set_code),
    )


def _fetch_podcast_items(feed_url: str) -> list[_Item]:
    resp = requests.get(feed_url, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

    items: list[_Item] = []
    for node in root.iterfind(".//item"):
        raw_title = _text(node, "title")
        published = _parse_rfc822(_text(node, "pubDate"))
        if published is None:
            continue
        enclosure = node.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None
        image_node = node.find("itunes:image", ns)
        items.append(
            _Item(
                guid=_text(node, "guid") or audio_url or raw_title,
                kind="episode",
                number=_episode_number(raw_title),
                title=_clean_title(raw_title),
                link=_text(node, "link"),
                summary=_strip_html(_text(node, "itunes:summary", ns) or _text(node, "description")),
                image=image_node.get("href", "") if image_node is not None else "",
                published_at=published,
                duration_seconds=_parse_duration(_text(node, "itunes:duration", ns)),
                audio_url=audio_url,
                youtube_id=None,
            )
        )
    return items


def _category_from_playlists(playlists: list[str]) -> str | None:
    lowered = [p.lower() for p in playlists]
    if any("set review" in p for p in lowered):
        return "Set Review"
    if any("coaching" in p or "draft class" in p for p in lowered):
        return "Coaching"
    if any("sealed" in p for p in lowered):
        return "Sealed"
    if any("top 10" in p or "tier list" in p or "ranking" in p for p in lowered):
        return "Rankings"
    if any(re.search(r"\bdraft\b", p) and "coaching" not in p and "class" not in p for p in lowered):
        return "Draft"
    if any("evergreen" in p or "mini level" in p for p in lowered):
        return "Evergreen"
    return None


_TITLE_OVERRIDES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Set Review", re.compile(r"first impressions?", re.I)),
    ("Metagame", re.compile(r"draft primer|draft guide|limited guide|win ?rate|tips for success", re.I)),
    ("Sealed", re.compile(r"pre-?release", re.I)),
)

_TITLE_CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Coaching", re.compile(r"coaching|draft class", re.I)),
    ("Guest", re.compile(
        r"conversation with|\bjoins (?:me|us)\b|\bft\.?\b|featuring|sits down with|\binterview\b",
        re.I,
    )),
    ("Rankings", re.compile(r"tier ?list|\branking\b|best and worst|props and slops|ranked every", re.I)),
    ("Set Review", re.compile(
        r"set review|set overview|card evaluation|primer|first impressions|first look",
        re.I,
    )),
    ("Sealed", re.compile(r"sealed|pre-?release", re.I)),
    ("Metagame", re.compile(
        r"state of the format|format address|format update|metagame|meta update"
        r"|mid-?format|what we got wrong|late format",
        re.I,
    )),
    ("Draft", re.compile(r"draft-?along|draft log|live draft|drafting with", re.I)),
)

_TOP_LIST = re.compile(r"\btop \d+", re.I)
_TOP_LIST_RANK_CONTEXT = re.compile(r"mythic|competitor|\bplayer\b|pro tour|\bpt\b|qualifier|arena direct", re.I)
_TOP_LIST_SKILL = re.compile(r"top \d+\s+(?:ways|tips|things|reasons|habits|mistakes|plays|lessons)", re.I)


def _category_from_title_overrides(title: str) -> str | None:
    for category, pattern in _TITLE_OVERRIDES:
        if pattern.search(title):
            return category
    return None


def _category_from_title(title: str) -> str | None:
    for category, pattern in _TITLE_CATEGORY_RULES:
        if pattern.search(title):
            return category
    if _TOP_LIST.search(title) and not _TOP_LIST_RANK_CONTEXT.search(title) and not _TOP_LIST_SKILL.search(title):
        return "Rankings"
    return None


def _episode_number(title: str) -> int | None:
    match = re.search(r"#\s*(\d+)", title)
    return int(match.group(1)) if match else None


def _clean_title(title: str) -> str:
    prefix = settings.podcast_title_prefix
    if not prefix:
        return title
    cleaned = re.sub(rf"^(?:{prefix})\s*#?\s*\d+\s*[:\-–]\s*", "", title, flags=re.I).strip()
    return cleaned or title


def _title_key(title: str) -> str:
    no_number = re.sub(r"#\s*\d+", "", title.lower())
    return re.sub(r"[^a-z0-9]+", " ", no_number).strip()


def _title_overlap(a: str, b: str) -> bool:
    words_a = {w for w in _title_key(a).split() if len(w) > 3}
    if not words_a:
        return False
    shared = sum(1 for w in _title_key(b).split() if w in words_a)
    return shared / len(words_a) >= 0.5


def _parse_duration(raw: str) -> int:
    if not raw:
        return 0
    if ":" not in raw:
        return int(raw) if raw.isdigit() else 0
    seconds = 0
    for part in raw.split(":"):
        seconds = seconds * 60 + (int(part) if part.isdigit() else 0)
    return seconds


def _parse_rfc822(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", value)).strip()


def _text(node: ET.Element, tag: str, ns: dict | None = None) -> str:
    found = node.find(tag, ns) if ns else node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""
