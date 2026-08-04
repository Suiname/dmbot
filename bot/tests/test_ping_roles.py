import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot import emojis
from bot.services.ping_roles import (
    EARLY_POD_ROLE_NAME,
    MANAGED_ROLES,
    REMINDER_COLOR,
    REMINDER_ROLE_NAME,
    MOCK_DRAFT_ROLE_NAME,
    PING_ROLES,
    LATE_POD_ROLE_NAME,
    WEEKEND_EARLY_POD_ROLE_NAME,
    WEEKEND_LATE_POD_ROLE_NAME,
    _ensure_managed_role,
    _first_welcome_for,
    _LinkArenaButton,
    auto_grant_spec_for_event,
    blurb_with_time,
    button_custom_id,
    forget_welcome,
    grant_mock_draft_role,
    grant_pod_roles,
)
from bot.services.pod_roles import consume_bot_umbrella_grant, grant_role
from bot.services.pod_schedule import POD_DRAFTERS_ROLE_NAME, SCHEDULE_TZ


def test_auto_grant_maps_timed_weekday_slots_to_their_roles():
    thursday = datetime(2026, 6, 11, 14, 0, tzinfo=SCHEDULE_TZ)
    wednesday = datetime(2026, 6, 10, 20, 0, tzinfo=SCHEDULE_TZ)
    off_grid = datetime(2026, 6, 9, 11, 0, tzinfo=SCHEDULE_TZ)

    assert auto_grant_spec_for_event(thursday).name == EARLY_POD_ROLE_NAME
    assert auto_grant_spec_for_event(wednesday).name == LATE_POD_ROLE_NAME
    assert auto_grant_spec_for_event(off_grid) is None


def test_auto_grant_splits_weekend_daytime_from_evening():
    saturday_early = datetime(2026, 6, 13, 14, 0, tzinfo=SCHEDULE_TZ)
    saturday_evening = datetime(2026, 6, 13, 21, 0, tzinfo=SCHEDULE_TZ)
    sunday_evening = datetime(2026, 6, 14, 20, 0, tzinfo=SCHEDULE_TZ)
    saturday_off_grid = datetime(2026, 6, 13, 20, 0, tzinfo=SCHEDULE_TZ)

    assert auto_grant_spec_for_event(saturday_early).name == WEEKEND_EARLY_POD_ROLE_NAME
    assert auto_grant_spec_for_event(saturday_evening).name == WEEKEND_LATE_POD_ROLE_NAME
    assert auto_grant_spec_for_event(sunday_evening).name == WEEKEND_LATE_POD_ROLE_NAME
    assert auto_grant_spec_for_event(saturday_off_grid) is None


def test_button_custom_id_is_a_stable_slug():
    assert button_custom_id(_spec_named(POD_DRAFTERS_ROLE_NAME)) == "role-toggle-pod-drafters"


def test_blurb_with_time_pairs_a_weekday_slot_with_its_local_time():
    blurb = blurb_with_time(_spec_named(EARLY_POD_ROLE_NAME))

    assert "<t:" in blurb and ":t>" in blurb


def test_blurb_with_time_lists_every_hour_the_weekend_lane_runs_at():
    early = blurb_with_time(_spec_named(WEEKEND_EARLY_POD_ROLE_NAME))
    late = blurb_with_time(_spec_named(WEEKEND_LATE_POD_ROLE_NAME))

    assert early.count("<t:") == 1
    assert late.count("<t:") == 2


def test_first_welcome_fires_once_until_forgotten():
    member_id = 90909

    assert _first_welcome_for(member_id) is True
    assert _first_welcome_for(member_id) is False
    forget_welcome(member_id)
    assert _first_welcome_for(member_id) is True


def test_consume_bot_umbrella_grant_is_a_one_shot_flag():
    grant_role_marks_umbrella_grant()

    assert consume_bot_umbrella_grant(4242) is True
    assert consume_bot_umbrella_grant(4242) is False


def test_bot_mediated_umbrella_grant_is_marked_so_the_listener_skips_it():
    member = _FakeMember(4343)
    umbrella = SimpleNamespace(name=POD_DRAFTERS_ROLE_NAME)

    asyncio.run(grant_role(member, umbrella))

    assert consume_bot_umbrella_grant(4343) is True


def test_slot_role_grant_leaves_the_umbrella_unmarked():
    member = _FakeMember(4444)
    slot_role = SimpleNamespace(name=EARLY_POD_ROLE_NAME)

    asyncio.run(grant_role(member, slot_role))

    assert consume_bot_umbrella_grant(4444) is False


def test_the_slot_role_is_skipped_when_the_player_switched_it_off(monkeypatch):
    monkeypatch.setattr("bot.services.ping_roles.declined_pod_roles_sync", lambda user_id: {"early"})
    member = _FakeMember(5151)

    asyncio.run(grant_pod_roles(member, EARLY_POD_ROLE_NAME))

    assert [role.name for role in member.roles] == [POD_DRAFTERS_ROLE_NAME]


def test_a_decline_on_one_slot_leaves_the_others_grantable(monkeypatch):
    monkeypatch.setattr("bot.services.ping_roles.declined_pod_roles_sync", lambda user_id: {"early"})
    member = _FakeMember(5252)

    asyncio.run(grant_pod_roles(member, LATE_POD_ROLE_NAME))

    assert [role.name for role in member.roles] == [POD_DRAFTERS_ROLE_NAME, LATE_POD_ROLE_NAME]


def test_pod_drafters_is_granted_whatever_the_player_declined(monkeypatch):
    monkeypatch.setattr(
        "bot.services.ping_roles.declined_pod_roles_sync",
        lambda user_id: {"drafters", "early", "late", "wknd_early", "wknd_late"},
    )
    member = _FakeMember(5353)

    asyncio.run(grant_pod_roles(member, None))

    assert [role.name for role in member.roles] == [POD_DRAFTERS_ROLE_NAME]


@pytest.mark.parametrize(
    "declined, held, expected",
    [
        (set(), [], [POD_DRAFTERS_ROLE_NAME, MOCK_DRAFT_ROLE_NAME]),
        ({"mock"}, [], [POD_DRAFTERS_ROLE_NAME]),
        (set(), [MOCK_DRAFT_ROLE_NAME], [MOCK_DRAFT_ROLE_NAME, POD_DRAFTERS_ROLE_NAME]),
    ],
    ids=["grants-on-join", "respects-the-decline", "already-held"],
)
def test_mock_draft_join_carries_the_umbrella(monkeypatch, declined, held, expected):
    monkeypatch.setattr("bot.services.ping_roles.declined_pod_roles_sync", lambda user_id: declined)
    member = _FakeMember(6161)
    member.roles = [role for role in member.guild.roles if role.name in held]

    asyncio.run(grant_mock_draft_role(member))

    assert [role.name for role in member.roles] == expected


def test_link_arena_button_has_no_emoji_when_the_app_emoji_is_not_uploaded(monkeypatch):
    """`emojis.get()` returns `""` for an app emoji this Discord application never uploaded, and Discord
    rejects a Button constructed with an empty-string emoji ("Invalid emoji"). This crashed every join
    confirmation card on a fresh fork until the button fell back to `None` instead."""
    monkeypatch.setattr(emojis, "_EMOJIS", {})

    assert _LinkArenaButton().emoji is None


def test_link_arena_button_shows_the_emoji_once_it_is_uploaded(monkeypatch):
    uploaded = MagicMock()
    uploaded.__str__.return_value = "<:mtga:1466076574372724819>"
    monkeypatch.setattr(emojis, "_EMOJIS", {"mtga": uploaded})

    assert _LinkArenaButton().emoji.name == "mtga"


def test_every_ping_role_carries_a_distinct_key():
    keys = [spec.key for spec in PING_ROLES]

    assert len(keys) == len(set(keys))
    assert all(key and key == key.lower() for key in keys)


class _FakeGuild:
    def __init__(self):
        self.roles = [SimpleNamespace(name=spec.name) for spec in PING_ROLES]


def grant_role_marks_umbrella_grant():
    member = _FakeMember(4242)
    umbrella = SimpleNamespace(name=POD_DRAFTERS_ROLE_NAME)
    asyncio.run(grant_role(member, umbrella))


class _FakeMember:
    def __init__(self, member_id):
        self.id = member_id
        self.roles = []
        self.guild = _FakeGuild()

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(roles)


def _spec_named(name):
    from bot.services.ping_roles import PING_ROLES

    for spec in PING_ROLES:
        if spec.name == name:
            return spec
    raise AssertionError(f"no spec named {name}")


class FakeManagedGuild:
    def __init__(self, *roles):
        self.name = "guild"
        self.roles = list(roles)
        self.created = []

    async def create_role(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(**kwargs)


class FakeManagedRole:
    def __init__(self, name, colour):
        self.name = name
        self.colour = colour

    async def edit(self, **kwargs):
        for field, value in kwargs.items():
            if field != "reason":
                setattr(self, field, value)


def test_a_renamed_managed_role_is_adopted_rather_than_orphaned():
    wanted = discord.Colour.from_str(REMINDER_COLOR)
    stale = FakeManagedRole("P0P1 Reminder", discord.Colour.default())
    guild = FakeManagedGuild(stale)

    asyncio.run(_ensure_managed_role(guild, managed_named(REMINDER_ROLE_NAME)))

    assert (stale.name, stale.colour) == (REMINDER_ROLE_NAME, wanted)
    assert guild.created == []


def test_a_managed_role_with_no_alias_present_is_created():
    guild = FakeManagedGuild()

    asyncio.run(_ensure_managed_role(guild, managed_named(REMINDER_ROLE_NAME)))

    assert [role["name"] for role in guild.created] == [REMINDER_ROLE_NAME]


def managed_named(name):
    for spec in MANAGED_ROLES:
        if spec.name == name:
            return spec
    raise AssertionError(f"{name} is not a managed role")
