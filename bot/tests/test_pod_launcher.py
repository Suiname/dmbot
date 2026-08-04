from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from bot.commands.pod_queue import _when_clock, _when_options
from bot.commands.test_group import HALL_OF_FAME
from bot.config import settings
from bot.services.pod_format_select import WRITE_IN_VALUE
from bot.services import pod_launch
from bot.services.pod_launch import LauncherSlot, _lazy_status
from bot.services.pod_signals import (
    LANE_LATE,
    RSVP_YES,
    SCHEDULE_TZ,
    STATUS_EXPIRED,
    STATUS_FIRED,
    STATUS_OPEN,
    named_bucket_key,
    next_lane_start,
)
from bot.sets import active_set_code
from bot.services.pod_launcher_copy import FINISHED_MARK
from bot.tasks.pod_daily_poll import (
    BOARD_LEAVE_ID,
    FIELD_VALUE_LIMIT,
    PLAYED_ROWS_KEPT,
    PodPollView,
    _cards_holding_user,
    _clamped_value,
    _column_value,
    _committed_card_link,
    _early_transition_is_live,
    _poll_channel,
    build_reminder_view,
    lane_settled_for_day,
)


BEFORE_EARLY = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
LATEST = active_set_code()


def _committed(bucket_key, thread_id, thread_message_id):
    return LauncherSlot(
        named_bucket_key(bucket_key, LATEST), committed=True, status=STATUS_FIRED, count=1,
        slot_time=None, set_code=LATEST,
        names=["p"], thread_id=thread_id, signal_id=None, thread_message_id=thread_message_id,
    )


def _lazy(bucket_key, status, set_code=None):
    set_code = set_code or LATEST
    return LauncherSlot(
        named_bucket_key(bucket_key, set_code), committed=False, status=status, count=0, slot_time=None,
        names=[], thread_id=None, signal_id=None, set_code=set_code,
    )


def test_committed_slot_deep_links_to_its_thread_card():
    view = PodPollView([_committed("EARLY", "555", "777")], guild=None)

    assert view.children[0].url.endswith("/555/777")


def test_committed_slot_without_a_card_links_to_the_thread_itself():
    view = PodPollView([_committed("EARLY", "555", None)], guild=None)

    assert view.children[0].url.endswith("/555")


def _committed_named(thread_name, card_channel_id, card_message_id, thread_id="555", thread_message_id="777"):
    return LauncherSlot(
        named_bucket_key("EARLY", LATEST), committed=True, status=STATUS_FIRED, count=1, slot_time=None,
        names=["p"], thread_id=thread_id, signal_id=None, thread_message_id=thread_message_id,
        card_message_id=card_message_id, card_channel_id=card_channel_id, thread_name=thread_name,
        set_code=LATEST,
    )


def test_committed_card_link_targets_the_coordination_card_by_name():
    name = "MSH Jul 21 Early Pod"
    slot = _committed_named(name, "111", "222")

    link = _committed_card_link(None, slot)

    assert link == f"[__**{name}**__](https://discord.com/channels/@me/111/222)"


def test_committed_card_link_falls_back_to_the_thread_without_a_card():
    name = "MSH Jul 21 Early Pod"
    slot = _committed_named(name, None, None)

    link = _committed_card_link(None, slot)

    assert link == f"[__**{name}**__](https://discord.com/channels/@me/555/777)"


def test_committed_card_link_is_none_without_a_thread_name():
    slot = _committed_named(None, "111", "222")

    assert _committed_card_link(None, slot) is None


def test_reminder_view_confirms_yes_or_no_and_opens_format_preference():
    view = build_reminder_view("EVT9")

    custom_ids = [child.custom_id for child in view.children]
    assert custom_ids == ["podreminderrsvp:yes:EVT9", "podreminderrsvp:no:EVT9", "podremindfmt:EVT9"]


def test_format_locked_reminder_view_drops_format_preference():
    view = build_reminder_view("EVT9", format_locked=True)

    custom_ids = [child.custom_id for child in view.children]
    assert custom_ids == ["podreminderrsvp:yes:EVT9", "podreminderrsvp:no:EVT9"]


def test_a_closed_slot_carries_no_join_button_while_an_open_one_does():
    view = PodPollView([_lazy("EARLY", STATUS_EXPIRED), _lazy("LATE", STATUS_OPEN)])

    pods = [child.custom_id for child in view.children if child.custom_id != BOARD_LEAVE_ID]

    assert pods == [f"pod_poll:{named_bucket_key('LATE', LATEST)}"]


def test_a_slot_offering_two_formats_carries_one_button_per_pod():
    view = PodPollView([_lazy("EARLY", STATUS_OPEN), _lazy("EARLY", STATUS_OPEN, set_code="PEASANT")])

    assert [child.custom_id for child in view.children] == [
        f"pod_poll:{named_bucket_key('EARLY', LATEST)}",
        f"pod_poll:{named_bucket_key('EARLY', 'PEASANT')}",
        BOARD_LEAVE_ID,
    ]


def test_the_board_carries_one_leave_for_every_pod_on_it():
    view = PodPollView([
        _lazy("EARLY", STATUS_OPEN),
        _lazy("EARLY", STATUS_OPEN, set_code="PEASANT"),
        _committed("LATE", "555", "777"),
    ])

    assert [child.custom_id for child in view.children].count(BOARD_LEAVE_ID) == 1


def test_a_board_with_nothing_left_to_leave_carries_no_leave_button():
    view = PodPollView([_lazy("EARLY", STATUS_EXPIRED)])

    assert BOARD_LEAVE_ID not in [child.custom_id for child in view.children]


LATE_TONIGHT = datetime(2026, 7, 18, 20, 0, tzinfo=SCHEDULE_TZ)
LATE_TOMORROW = LATE_TONIGHT + timedelta(days=1)


def _late_slot(slot_time, *, committed, finished=False, status=STATUS_OPEN, set_code=None):
    set_code = set_code or LATEST
    return LauncherSlot(
        named_bucket_key("LATE", set_code), committed=committed, status=status, count=8,
        slot_time=slot_time, names=[], thread_id=None, signal_id=None, set_code=set_code,
        finished=finished,
    )


@pytest.mark.parametrize("slots, settled", [
    ([_late_slot(LATE_TONIGHT, committed=True, finished=True, status=STATUS_FIRED)], True),
    ([_late_slot(LATE_TONIGHT, committed=True, status=STATUS_FIRED)], False),
    ([_late_slot(LATE_TONIGHT, committed=False, status=STATUS_EXPIRED)], True),
    ([_late_slot(LATE_TONIGHT, committed=False, status=STATUS_OPEN)], False),
    ([
        _late_slot(LATE_TONIGHT, committed=True, finished=True, status=STATUS_FIRED),
        _late_slot(LATE_TONIGHT, committed=True, set_code="PEASANT", status=STATUS_FIRED),
    ], False),
    ([
        _late_slot(LATE_TONIGHT, committed=True, finished=True, status=STATUS_FIRED),
        _late_slot(LATE_TOMORROW, committed=False, status=STATUS_OPEN),
    ], True),
    ([_late_slot(LATE_TOMORROW, committed=False, status=STATUS_OPEN)], False),
])
def test_a_lane_settles_only_once_every_pod_of_that_day_is_done(slots, settled):
    assert lane_settled_for_day(slots, LANE_LATE, LATE_TONIGHT.date()) is settled


EARLY_TODAY = datetime(2026, 7, 18, 14, 0, tzinfo=SCHEDULE_TZ)
MORNING_BOARD = str(discord.utils.time_snowflake(datetime(2026, 7, 18, 10, 0, tzinfo=SCHEDULE_TZ)))
RESURFACED_BOARD = str(discord.utils.time_snowflake(datetime(2026, 7, 18, 17, 0, tzinfo=SCHEDULE_TZ)))


def _early_slot(*, committed, finished=False, status=STATUS_OPEN):
    return LauncherSlot(
        named_bucket_key("EARLY", LATEST), committed=committed, status=status, count=8,
        slot_time=EARLY_TODAY, names=[], thread_id=None, signal_id=None, set_code=LATEST,
        finished=finished,
    )


EARLY_PLAYED = _early_slot(committed=True, finished=True, status=STATUS_FIRED)
EARLY_PLAYING = _early_slot(committed=True, status=STATUS_FIRED)
LATE_GATHERING = _late_slot(LATE_TONIGHT, committed=False, status=STATUS_OPEN)
LATE_PLAYED = _late_slot(LATE_TONIGHT, committed=True, finished=True, status=STATUS_FIRED)


@pytest.mark.parametrize("slots, message_id, live", [
    ([EARLY_PLAYED, LATE_GATHERING], MORNING_BOARD, True),
    ([EARLY_PLAYING, LATE_GATHERING], MORNING_BOARD, False),
    ([EARLY_PLAYED, LATE_PLAYED], MORNING_BOARD, False),
    ([EARLY_PLAYED, LATE_GATHERING], RESURFACED_BOARD, False),
    ([LATE_GATHERING], MORNING_BOARD, False),
])
def test_the_early_transition_is_live_only_between_the_two_pods_of_a_day(slots, message_id, live):
    assert _early_transition_is_live(slots, EARLY_TODAY.date(), message_id) is live


def test_a_pod_that_started_drafting_carries_no_button():
    drafting = replace(_committed("EARLY", "555", "777"), locked=True)

    view = PodPollView([drafting])

    assert view.children == []


@pytest.mark.parametrize(
    "status, slot_hour, expected",
    [
        (STATUS_OPEN, 10, STATUS_EXPIRED),
        (STATUS_OPEN, 20, STATUS_OPEN),
        (STATUS_FIRED, 10, STATUS_FIRED),
    ],
)
def test_lazy_status_closes_only_a_passed_open_slot(status, slot_hour, expected):
    slot_time = datetime(2026, 7, 18, slot_hour, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)

    assert _lazy_status(status, slot_time, now) == expected


def test_when_options_default_right_now_when_unscheduled():
    options = _when_options(None, BEFORE_EARLY)

    defaulted = [o for o in options if o.default]
    assert [o.value for o in defaulted] == ["now"]
    assert options[-1].value == WRITE_IN_VALUE


def test_when_options_defaults_the_selected_preset():
    late = next_lane_start(LANE_LATE, BEFORE_EARLY)

    options = _when_options(late, BEFORE_EARLY)

    defaulted = [o for o in options if o.default]
    assert [o.value for o in defaulted] == ["LATE"]
    assert options[-1].value == WRITE_IN_VALUE


def test_when_options_shows_custom_time_as_its_own_defaulted_option():
    custom = datetime(2026, 7, 18, 21, 0, tzinfo=timezone.utc)

    options = _when_options(custom, BEFORE_EARLY)

    defaulted = [o for o in options if o.default]
    assert len(defaulted) == 1
    assert defaulted[0].value == WRITE_IN_VALUE
    assert _when_clock(custom) in defaulted[0].label


def test_seed_options_from_rankings_adds_each_ranked_set():
    from bot.services.pod_draft_manager import _seed_options_from_rankings

    options = ["MSH", "FLASH", "NEO"]
    rankings = (("1", ("NEO", "IKO")), ("2", ("NEO", "MH3")))

    _seed_options_from_rankings(options, rankings)

    assert options == ["MSH", "FLASH", "NEO", "IKO", "MH3"]


def test_seed_options_from_rankings_respects_the_option_cap():
    from bot.services import pod_format_poll
    from bot.services.pod_draft_manager import _seed_options_from_rankings

    options = [f"S{i}" for i in range(pod_format_poll.MAX_ROWED_OPTIONS)]
    rankings = (("1", ("NEW1", "NEW2")),)

    _seed_options_from_rankings(options, rankings)

    assert len(options) == pod_format_poll.MAX_ROWED_OPTIONS
    assert "NEW1" not in options


def _played(index):
    return replace(
        _committed("EARLY", f"11{index}", f"22{index}"), finished=True, locked=True,
        winner=HALL_OF_FAME[index], thread_name=f"Peasant Cube Aug {index + 1} Early Pod",
        slot_time=BEFORE_EARLY, card_message_id=f"33{index}", card_channel_id="444",
    )


def _gathering_with_roster(size):
    return replace(
        _lazy("EARLY", STATUS_OPEN), count=size, slot_time=BEFORE_EARLY + timedelta(days=1),
        names=list(HALL_OF_FAME[:size]),
    )


@pytest.mark.parametrize("played_pods,rostered", [(1, 4), (3, 8), (12, 16), (30, 30)])
def test_a_column_stays_inside_an_embed_field(played_pods, rostered):
    """A day of played pods, each carrying a thread link and a winner seat link, plus a committed pod for
    tomorrow, ran past the field limit and Discord refused the whole board edit."""
    column = [_played(index) for index in range(played_pods)] + [_gathering_with_roster(rostered)]

    value = _column_value(column, guild=None)

    assert len(value) <= FIELD_VALUE_LIMIT


def test_the_played_rows_past_the_cap_collapse_into_one_count_line():
    column = [_played(index) for index in range(6)] + [_gathering_with_roster(8)]

    value = _column_value(column, guild=None)

    assert value.count(FINISHED_MARK) == PLAYED_ROWS_KEPT
    assert all(_played(index).winner in value for index in (4, 5))
    assert "https://" in value


def test_a_value_past_the_field_limit_is_cut_on_a_line_boundary():
    value = _clamped_value("\n".join(["x" * 100] * 20))

    assert len(value) <= FIELD_VALUE_LIMIT
    assert value.endswith("x" * 100)


def _committed_championship(thread_id="555", card_message_id="900"):
    return LauncherSlot(
        "AFTERNOON", committed=True, status=STATUS_FIRED, count=8, slot_time=None,
        names=["a", "b"], thread_id=thread_id, signal_id=None, thread_message_id="777",
        card_message_id=card_message_id, thread_name="👑 MSH Set Championship", championship=True,
    )


def test_championship_slot_is_a_link_not_an_rsvp_toggle():
    view = PodPollView([_committed_championship()], guild=None)

    custom_ids = [child.custom_id for child in view.children if getattr(child, "custom_id", None)]
    link_buttons = [child for child in view.children if getattr(child, "url", None)]

    assert not any(cid.startswith("pod_slot_rsvp") for cid in custom_ids)
    assert link_buttons and link_buttons[0].url.endswith("/555/777")


def test_a_championship_lane_keeps_the_pods_its_column_already_played():
    """The eve of a championship, where the column has played its own pods and also points at tomorrow's
    event: the pointer is a block below them and never a replacement for the column."""
    played = replace(
        _committed("EARLY", "111", "222"), finished=True, locked=True, winner="Finkel",
        thread_name="MSH Jul 31 Early Pod", slot_time=BEFORE_EARLY,
    )

    value = _column_value([played, _committed_championship()], guild=None)

    assert "/111/222" in value
    assert "/555/777" in value


def test_a_board_leave_never_reaches_a_championship_seat(monkeypatch):
    """The launcher only points at a championship, which is answered on its own card. A Leave pressed to get
    off tonight's pod must not withdraw a seat, and a board carrying nothing else needs no Leave at all."""
    monkeypatch.setattr(
        pod_launch, "card_rsvp_for_user_sync", lambda card_message_id, discord_user_id: RSVP_YES,
    )
    championship_slot = _committed_championship()
    pod = replace(_committed("LATE", "111", "222"), card_message_id="333", card_channel_id="444")

    held = _cards_holding_user([championship_slot, pod], "u1")
    view = PodPollView([championship_slot])

    assert [slot.thread_id for slot in held] == ["111"]
    assert BOARD_LEAVE_ID not in [child.custom_id for child in view.children]


def test_poll_channel_rejects_a_channel_that_cannot_send_messages(monkeypatch):
    """A Forum channel (or any non-text channel) has no `.send()`. `POD_DRAFT_CHANNEL_ID` pointed at one
    of these on 2026-08-03 and crashed the daily launcher instead of failing gracefully."""
    monkeypatch.setattr(settings, "pod_draft_channel_id", 999)
    forum_channel = SimpleNamespace(id=999)

    assert _poll_channel(SimpleNamespace(get_channel=lambda cid: forum_channel)) is None


def test_poll_channel_resolves_a_configured_text_channel(monkeypatch):
    monkeypatch.setattr(settings, "pod_draft_channel_id", 999)
    text_channel = MagicMock(spec=discord.TextChannel)

    assert _poll_channel(SimpleNamespace(get_channel=lambda cid: text_channel)) is text_channel
