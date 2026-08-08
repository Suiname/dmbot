"""MTG Scribe's event calendar, served from a bundled snapshot, plus the REST client that captures it.

``scribe_calendar.json`` is what the bot reads — ``load_events`` never touches the network. MTG Scribe
publishes each set's queues weeks ahead and the schedule holds for the window, so fetching per
invocation bought little and cost seconds: three sequential pages, capped at 50 events each, on a host
whose account-level anti-bot (``/.well-known/sgcaptcha/``) intermittently challenges datacenter egress
with a 202 HTML page. ``bot.scripts.snapshot_scribe`` refreshes the file from a clean IP, so a Scribe
correction reaches players on the next deploy and nothing degrades when the site blocks us.

``fetch_raw_events`` is the only code path that requests anything from mtgscribe.com, and only
``snapshot_scribe`` calls it. The Events Calendar endpoint returns each queue with
``utc_start_date``/``utc_end_date``; the stock ``/events/feed/`` RSS only carries a start date, so the
REST endpoint is the one worth consuming.

Queues that share a set and a calendar-day window collapse into one group: the three Secrets of
Strixhaven drafts become a single ``EventGroup`` listing all three formats, instead of three
near-duplicate callouts. Grouping keys on the date, not the timestamp, so queues that open an hour
apart still merge.
"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from bot.config import settings

logger = logging.getLogger(__name__)

EVENTS_URL = "https://mtgscribe.com/wp-json/tribe/events/v1/events"
CALENDAR_PATH = Path(__file__).resolve().parent / "scribe_calendar.json"
STALE_HORIZON = timedelta(days=14)
PER_PAGE = 50
REQUEST_TIMEOUT = 15
REQUEST_HEADERS = {
    "User-Agent": f"{settings.community_name}-Bot/1.0 (+{settings.public_site_url})",
    "Accept": "application/json",
}


class ScribeUnavailable(RuntimeError):
    """The upstream returned a 2xx whose body was not JSON — a bot-challenge or block page."""


@dataclass(frozen=True)
class ScribeEvent:
    title: str
    format_label: str
    group_label: str
    start: datetime
    end: datetime
    start_local: datetime
    end_local: datetime
    tag_slugs: tuple[str, ...]


@dataclass
class EventGroup:
    label: str
    formats: list[str] = field(default_factory=list)
    start: datetime = None
    end: datetime = None
    start_local: datetime = None
    end_local: datetime = None
    flashback: bool = False
    cube: bool = False
    competitive: bool = False
    limited_format: str = ""


ARENA_TAG = "arena"
FLASHBACK_TAG = "flashback"
CUBE_TAG = "cube"
DRAFT_TAG = "draft"
SEALED_TAGS = ("sealed", "traditional-sealed")
COMPETITIVE_TAGS = ("play-in", "qualifier", "arena-championship", "arena-limited-championship-qualifier",
                    "arena-open")


def load_events(*, arena_only: bool = True) -> list[ScribeEvent]:
    """Every event in the bundled calendar. No network, so this is the read every command and tick uses.

    ``arena_only`` keeps MTG Arena client events (the ``arena`` tag) and drops tabletop
    programs. The Limited-vs-Constructed cut is left to the caller, so the Midweek view
    can surface constructed queues.
    """
    events = _parse_event_dicts(_load_calendar(), arena_only)
    _warn_if_stale(events)
    return events


def fetch_raw_events(start_date: date) -> list[dict]:
    """Every raw event dict starting on/after ``start_date``, following pagination — the one code path
    that requests anything from mtgscribe.com, called only by ``bot.scripts.snapshot_scribe``.

    The API caps each page at 50 regardless of ``per_page`` and reports ``total_pages``; an explicit
    past ``start_date`` is required to surface in-progress events, whose start has already passed and
    which the default (today-onward) window drops. The ``_cb`` cache-bust makes every capture read
    origin, since the CDN otherwise serves stale dates for the canonical URL.
    """
    raw_events: list[dict] = []
    cache_bust = time.strftime("%Y%m%d%H%M%S")
    page = 1
    while True:
        payload = _get_page(start_date, page, cache_bust)
        batch = payload.get("events", [])
        raw_events.extend(batch)
        total_pages = payload.get("total_pages", page)
        if page >= total_pages or not batch:
            break
        page += 1
    return raw_events


def _load_calendar() -> list[dict]:
    with CALENDAR_PATH.open(encoding="utf-8") as calendar:
        return json.load(calendar)


def _parse_event_dicts(raw_events: list[dict], arena_only: bool) -> list[ScribeEvent]:
    events = [_parse_event(raw) for raw in raw_events]
    if arena_only:
        return [event for event in events if ARENA_TAG in event.tag_slugs]
    return events


def _warn_if_stale(events: list[ScribeEvent]) -> None:
    """The calendar is the only source, so a skipped ``snapshot_scribe`` run would otherwise decay
    into an empty schedule with nothing saying why."""
    if not events:
        logger.warning(f"{CALENDAR_PATH.name} holds no events; run bot.scripts.snapshot_scribe")
        return
    latest_end = max(event.end for event in events)
    if latest_end - datetime.now(timezone.utc) < STALE_HORIZON:
        logger.warning(f"{CALENDAR_PATH.name} runs out on {latest_end:%Y-%m-%d}; "
                       "run bot.scripts.snapshot_scribe and deploy")


def group_events(events: list[ScribeEvent]) -> list[EventGroup]:
    groups: dict[tuple, EventGroup] = {}
    for event in events:
        key = (event.group_label, event.start.date(), event.end.date())
        group = groups.get(key)
        if group is None:
            group = EventGroup(
                label=event.group_label,
                start=event.start,
                end=event.end,
                start_local=event.start_local,
                end_local=event.end_local,
            )
            groups[key] = group
        if event.format_label and event.format_label not in group.formats:
            group.formats.append(event.format_label)
        if FLASHBACK_TAG in event.tag_slugs:
            group.flashback = True
        if any(CUBE_TAG in tag for tag in event.tag_slugs):
            group.cube = True
        if any(tag in COMPETITIVE_TAGS for tag in event.tag_slugs):
            group.competitive = True
        if not group.limited_format:
            group.limited_format = _limited_format(event.tag_slugs)
    return list(groups.values())


def _limited_format(tag_slugs: tuple[str, ...]) -> str:
    """Sealed or Draft, from the tags. A competitive callout has to name the format, and the title
    carries it only sometimes — "ACQ Play-In: Bo1 The Hobbit Sealed" spells it out, "Arena Open: The
    Hobbit" does not. Empty for a queue whose tags say neither."""
    if any(tag in SEALED_TAGS for tag in tag_slugs):
        return "Sealed"
    if any(DRAFT_TAG in tag for tag in tag_slugs):
        return "Draft"
    return ""


def partition_by_now(groups: list[EventGroup], now: datetime) -> tuple[list[EventGroup], list[EventGroup]]:
    """Split groups into (in-progress, upcoming), dropping anything already ended.

    In-progress leads with the latest end (most time left at the top); upcoming leads
    with whatever begins next.
    """
    in_progress: list[EventGroup] = []
    upcoming: list[EventGroup] = []
    for group in groups:
        if group.end < now:
            continue
        if group.start <= now:
            in_progress.append(group)
        else:
            upcoming.append(group)
    in_progress.sort(key=lambda group: group.end, reverse=True)
    upcoming.sort(key=lambda group: group.start)
    return in_progress, upcoming


def _get_page(start_date: date, page: int, cache_bust: str | None) -> dict:
    """``_cb`` busts MTG Scribe's CDN cache, which keys on the query string and otherwise
    serves stale dates (a Cache-Control header doesn't reach origin, a unique param does)."""
    params = {"start_date": start_date.isoformat(), "per_page": PER_PAGE, "page": page}
    if cache_bust:
        params["_cb"] = cache_bust
    response = requests.get(EVENTS_URL, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        snippet = response.text[:200].replace("\n", " ")
        raise ScribeUnavailable(
            f"non-JSON body from {response.url} (status {response.status_code}, "
            f"content-type {response.headers.get('content-type')!r}): {snippet!r}"
        ) from exc


def _parse_event(raw: dict) -> ScribeEvent:
    title = html.unescape(raw.get("title", "")).strip()
    format_label, group_label = _split_title(title)
    tag_slugs = tuple(tag.get("slug", "") for tag in raw.get("tags", []))
    return ScribeEvent(
        title=title,
        format_label=format_label,
        group_label=group_label,
        start=_parse_utc(raw["utc_start_date"]),
        end=_parse_utc(raw["utc_end_date"]),
        start_local=_parse_naive(raw["start_date"]),
        end_local=_parse_naive(raw["end_date"]),
        tag_slugs=tag_slugs,
    )


def _split_title(title: str) -> tuple[str, str]:
    """Titles read ``"<format>: <set>"`` ("Premier Draft: Secrets of Strixhaven").

    The set is the grouping label; the format is what gets listed under it. Titles
    without a colon (release weekends, prereleases) carry no format and group on the
    whole title.
    """
    if ": " in title:
        format_label, group_label = title.split(": ", 1)
        return format_label, group_label
    return "", title


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _parse_naive(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
