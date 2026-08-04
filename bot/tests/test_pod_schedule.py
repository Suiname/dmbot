from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from bot.commands.pod_schedule import coordination_url
from bot.config import settings
from bot.services.pod_reminder_copy import RECRUITING_SECOND_TABLE
from bot.services.pod_schedule import (
    SCHEDULE_TZ,
    build_recruiting_message,
    highest_event_number,
    short_event_name,
    slots_for_week,
)


FLOOR = 6
AIM = 8


def test_slots_for_week_returns_wednesday_thursday_and_saturday_eastern():
    slots = slots_for_week(date(2026, 6, 8))

    assert slots == [
        datetime(2026, 6, 10, 20, 0, tzinfo=SCHEDULE_TZ),
        datetime(2026, 6, 11, 14, 0, tzinfo=SCHEDULE_TZ),
        datetime(2026, 6, 13, 15, 0, tzinfo=SCHEDULE_TZ),
    ]
    assert all(slot.utcoffset().total_seconds() == -4 * 3600 for slot in slots)


def test_slots_for_week_tracks_dst_end():
    slots = slots_for_week(date(2026, 11, 2))

    assert all(slot.utcoffset().total_seconds() == -5 * 3600 for slot in slots)


def test_highest_event_number_takes_the_max_and_ignores_unnumbered_names():
    names = ["SOS Pod Draft #3 - May 15", "SOS Pod Draft #5 - May 22", "SOS Pod Draft - aborted"]

    assert highest_event_number(names) == 5


def test_highest_event_number_defaults_to_zero_with_no_numbers():
    assert highest_event_number([]) == 0
    assert highest_event_number(["Pod Draft - no number"]) == 0


def test_recruiting_message_carries_the_short_name_the_relative_time_and_the_signup_link():
    event_time = datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)
    jump_url = "https://discord.com/channels/1/2/3"

    body = build_recruiting_message("FIN Pod Draft #1 - Jun 24", 4, FLOOR, AIM, event_time, jump_url)

    assert "FIN Pod Draft #1" in body
    assert "Jun 24" not in body
    assert f"<t:{int(event_time.timestamp())}:R>" in body
    assert ":F>" not in body
    assert f"]({jump_url})" in body


def test_recruiting_message_never_pings_a_role():
    event_time = datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)

    body = build_recruiting_message("Pod", 7, FLOOR, AIM, event_time, "url")

    assert "<@&" not in body


@pytest.mark.parametrize("count", [0, 1, 5, 6, 7, 8, 9, 12])
def test_recruiting_message_never_counts_down_past_a_number_already_reached(count):
    event_time = datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)

    body = build_recruiting_message("Pod", count, FLOOR, AIM, event_time, "url")

    assert "-1" not in body
    assert " 0 " not in body


def test_recruiting_message_reads_differently_below_the_floor_above_it_and_at_the_aim():
    event_time = datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)
    jump_url = "https://discord.com/channels/1/2/3"

    bodies = [
        build_recruiting_message("Pod", count, FLOOR, AIM, event_time, jump_url)
        for count in (FLOOR - 2, FLOOR, AIM)
    ]

    assert len(set(bodies)) == 3
    assert all(f"]({jump_url})" in body for body in bodies)


def test_recruiting_message_calls_out_a_second_table_only_once_yes_plus_maybe_reach_two():
    event_time = datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)

    one_short = build_recruiting_message("Pod", 9, FLOOR, AIM, event_time, "url", maybe_count=6)
    two_tables = build_recruiting_message("Pod", 10, FLOOR, AIM, event_time, "url", maybe_count=6)

    assert RECRUITING_SECOND_TABLE not in one_short
    assert RECRUITING_SECOND_TABLE in two_tables


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MSH Pod Draft #2 - Jun 25", "MSH Pod Draft #2"),
        ("SOS Pod Draft #3 - May 5", "SOS Pod Draft #3"),
        ("MSH Pod Draft #2", "MSH Pod Draft #2"),
        ("Throwback Cube Night", "Throwback Cube Night"),
    ],
)
def test_short_event_name_strips_trailing_date(name, expected):
    assert short_event_name(name) == expected


def test_coordination_url_uses_the_actual_guild_when_one_is_given():
    guild = SimpleNamespace(id=42)

    assert f"/42/" in coordination_url(guild)


def test_coordination_url_falls_back_to_production_guild_from_a_dm(monkeypatch):
    """A DM carries no guild, so the schedule embed's title link (posted by a DM-triggered task) has to
    fall back to the configured production guild instead of crashing on `guild.id`."""
    monkeypatch.setattr(settings, "production_guild_id", 987654321)

    assert f"/987654321/" in coordination_url(None)
