from datetime import datetime, timedelta, timezone

import discord
import pytest

from bot.services.p0p1_contest import Contest
from bot.services.p0p1_copy import reminder, reminder_view
from bot.config import settings
from bot.services.p0p1_reminder import SOURCE_PLAYERS, audience, non_voter_discord_ids_sync

_PROD_ID = 111111111
from bot.services.ping_roles import P0P1_COLOR
from bot.tasks.p0p1_reminder_post import REMINDER_LEAD, contest_due


def make_contest(code="HOB", name="The Hobbit", *, opens_days_before=10, deadline_hour=16) -> Contest:
    deadline = datetime(2026, 8, 5, deadline_hour, tzinfo=timezone.utc)
    return Contest(
        code=code, name=name,
        release=deadline + timedelta(days=6),
        previews_open=deadline - timedelta(days=opens_days_before),
        voting_deadline=deadline,
    )


@pytest.fixture
def contest():
    return make_contest()


def test_reminder_window_opens_exactly_one_day_before_the_deadline(monkeypatch, contest):
    monkeypatch.setattr("bot.tasks.p0p1_reminder_post.p0p1_contest.all_contests", lambda: [contest])
    fire_at = contest.voting_deadline - REMINDER_LEAD

    assert contest_due(fire_at - timedelta(seconds=1)) is None
    assert contest_due(fire_at) is contest


@pytest.mark.parametrize("offset,due", [
    (-timedelta(days=3), False),
    (-timedelta(hours=23), True),
    (-timedelta(minutes=1), True),
    (timedelta(0), False),
    (timedelta(hours=1), False),
])
def test_contest_is_due_only_inside_its_final_voting_day(monkeypatch, contest, offset, due):
    monkeypatch.setattr("bot.tasks.p0p1_reminder_post.p0p1_contest.all_contests", lambda: [contest])

    found = contest_due(contest.voting_deadline + offset)

    assert (found is not None) == due


def test_a_contest_shorter_than_the_lead_is_due_from_the_moment_it_opens(monkeypatch):
    short = make_contest(opens_days_before=0)
    short = Contest(
        code=short.code, name=short.name, release=short.release,
        previews_open=short.voting_deadline - timedelta(hours=6),
        voting_deadline=short.voting_deadline,
    )
    monkeypatch.setattr("bot.tasks.p0p1_reminder_post.p0p1_contest.all_contests", lambda: [short])

    assert contest_due(short.previews_open) is short
    assert contest_due(short.previews_open - timedelta(seconds=1)) is None


def test_a_contest_still_in_previews_is_not_due(monkeypatch, contest):
    monkeypatch.setattr("bot.tasks.p0p1_reminder_post.p0p1_contest.all_contests", lambda: [contest])

    assert contest_due(contest.previews_open - timedelta(hours=1)) is None


def test_the_role_mention_sits_outside_the_card(contest):
    """The ping addresses the reader; folding it into the container would read as part of the post."""
    view = reminder_view(contest, featured_code="HOB", role_mention="<@&99>", voter_count=74)

    outside, inside = _split_view(view)

    assert "<@&99>" in outside
    assert "<@&99>" not in inside


def test_reminder_card_links_the_ballot(contest):
    view = reminder_view(contest, featured_code="HOB", role_mention="<@&99>", voter_count=74)

    _, inside = _split_view(view)

    assert "/p0p1" in inside


@pytest.mark.parametrize("count,expected", [
    (1, "-# 1 player has voted so far"),
    (2, "-# 2 players have voted so far"),
    (74, "-# 74 players have voted so far"),
])
def test_turnout_rides_as_subtext_and_agrees_in_number(contest, count, expected):
    container = reminder(contest, featured_code="HOB", voter_count=count)

    assert expected in _container_text(container)


@pytest.mark.parametrize("count", [0, -1])
def test_no_turnout_line_when_nobody_has_voted(contest, count):
    container = reminder(contest, featured_code="HOB", voter_count=count)

    assert "-#" not in _container_text(container)


def test_headline_is_the_same_whatever_the_turnout(contest):
    headlines = set()
    for count in (0, 1, 74):
        body = _container_text(reminder(contest, featured_code="HOB", voter_count=count))
        headlines.add(next(line for line in body.splitlines() if line.startswith("## ")))

    assert headlines == {"## The Hobbit Pack 0 Pick 1 Challenge closes soon"}


def test_card_wears_the_p0p1_accent(contest):
    container = reminder(contest, featured_code="HOB", voter_count=74)

    assert container.accent_colour == discord.Color.from_str(P0P1_COLOR)


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.name = "guild"


class FakeMember:
    def __init__(self, member_id):
        self.id = member_id

    def __str__(self):
        return f"member-{self.id}"


EVERYONE = ["1", "2", "3"]


def test_the_production_guild_pings_the_whole_non_voter_list(monkeypatch):
    monkeypatch.setattr(settings, "production_guild_id", _PROD_ID)
    targeted = audience(FakeGuild(_PROD_ID), EVERYONE, FakeMember(42))

    assert targeted == EVERYONE


def test_any_other_guild_pings_only_whoever_asked(monkeypatch):
    monkeypatch.setattr(settings, "production_guild_id", _PROD_ID)
    targeted = audience(FakeGuild(_PROD_ID + 1), EVERYONE, FakeMember(42))

    assert targeted == ["42"]


def test_the_scheduled_tick_pings_nobody_outside_the_production_guild(monkeypatch):
    """No invoker means the hourly tick, which must never sweep a test server full of real people."""
    monkeypatch.setattr(settings, "production_guild_id", _PROD_ID)
    targeted = audience(FakeGuild(_PROD_ID + 1), EVERYONE, None)

    assert targeted == []


def test_a_database_without_the_auth_schema_reports_the_fallback_instead_of_raising(clean_db):
    """Every developer database lacks Supabase's `auth` schema; before the fallback this raised and the
    command died with no reply at all."""
    non_voters = non_voter_discord_ids_sync("HOB")

    assert non_voters.source == SOURCE_PLAYERS


def _split_view(view) -> tuple[str, str]:
    """(text outside any container, text inside the card)."""
    outside, inside = [], []
    for item in view.children:
        if isinstance(item, discord.ui.Container):
            inside.append(_container_text(item))
        elif isinstance(getattr(item, "content", None), str):
            outside.append(item.content)
    return "\n".join(outside), "\n".join(inside)


def _container_text(container) -> str:
    blocks = []
    for item in container.children:
        if isinstance(getattr(item, "content", None), str):
            blocks.append(item.content)
        for child in getattr(item, "children", []) or []:
            if isinstance(getattr(child, "content", None), str):
                blocks.append(child.content)
    return "\n".join(blocks)
