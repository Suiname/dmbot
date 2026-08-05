import asyncio

import discord

from bot.services import pod_draft_manager
from bot.services.lobby_embed import ready_check_confirm_text, ready_status_banner, roster_hold_detail
from bot.services.pod_draft_manager import PodDraftManager


def _ready_manager(user_ids: list[str]) -> tuple[PodDraftManager, list[bool]]:
    mgr = PodDraftManager(object(), "evt", "sid", 123, "SOS", len(user_ids))
    mgr.session_users = [{"userID": uid, "userName": uid} for uid in user_ids]
    mgr.expected_user_ids = set(user_ids)
    mgr.expected_user_names = {uid: uid for uid in user_ids}
    mgr.ready_socket_ids = set(user_ids)
    mgr.ready_check_active = True
    completed: list[bool] = []

    async def _record_complete():
        completed.append(True)
        mgr.ready_check_active = False

    mgr._complete_ready_check = _record_complete
    return mgr, completed


def _stub_lobby_io(mgr: PodDraftManager) -> None:
    async def _async_noop(*args, **kwargs):
        return None

    mgr.bot_user_id = "bot"
    mgr._refresh_lobby_status = _async_noop
    mgr.refresh_lobby_now = _async_noop
    mgr._admit_lobby_arrivals = lambda *args, **kwargs: None
    mgr._sync_leaderboard_seeding = lambda *args, **kwargs: None


def test_completes_when_all_present_and_ready():
    mgr, completed = _ready_manager([str(i) for i in range(8)])

    asyncio.run(mgr._maybe_complete_ready_check())

    assert completed == [True]


def test_ready_users_unions_both_surfaces():
    mgr, completed = _ready_manager([str(i) for i in range(4)])
    mgr.ready_socket_ids = {"0", "1"}
    mgr.ready_discord_ids = {"2"}

    assert mgr.ready_users == {"0", "1", "2"}

    asyncio.run(mgr._maybe_complete_ready_check())
    assert completed == []

    mgr.ready_discord_ids.add("3")
    asyncio.run(mgr._maybe_complete_ready_check())
    assert completed == [True]


def test_a_held_check_never_starts_the_draft():
    mgr, completed = _ready_manager([str(i) for i in range(8)])
    mgr.ready_check_left_ids = {"7"}

    asyncio.run(mgr._maybe_complete_ready_check())

    assert completed == []


def test_a_departure_holds_the_check_and_keeps_the_answers():
    mgr, completed = _ready_manager([str(i) for i in range(8)])
    _stub_lobby_io(mgr)

    asyncio.run(mgr._on_session_users([{"userID": str(i), "userName": str(i)} for i in range(7)]))

    assert completed == []
    assert mgr.ready_check_active is True
    assert mgr.ready_check_left_ids == {"7"}
    assert mgr.ready_users == {str(i) for i in range(8)}


def test_an_arrival_holds_the_check_rather_than_resizing_the_pod():
    mgr, completed = _ready_manager([str(i) for i in range(8)])
    _stub_lobby_io(mgr)

    arrived = [{"userID": str(i), "userName": str(i)} for i in range(9)]
    asyncio.run(mgr._on_session_users(arrived))

    assert completed == []
    assert mgr.ready_check_joined_ids == {"8"}
    assert "8" not in mgr.expected_user_ids


def test_the_hold_releases_and_starts_when_the_player_comes_back():
    mgr, completed = _ready_manager([str(i) for i in range(8)])
    _stub_lobby_io(mgr)
    asyncio.run(mgr._on_session_users([{"userID": str(i), "userName": str(i)} for i in range(7)]))

    asyncio.run(mgr._on_session_users([{"userID": str(i), "userName": str(i)} for i in range(8)]))

    assert mgr.is_ready_check_held() is False
    assert completed == [True]


def test_re_arming_keeps_the_answers_of_seats_still_in_the_lobby():
    mgr = _setup_manager([str(i) for i in range(8)])
    mgr.ready_check_active = True
    mgr.ready_discord_ids = {"0", "1"}
    mgr.ready_socket_ids = {"2", "7"}
    mgr.session_users = [{"userID": str(i), "userName": str(i)} for i in range(7)]
    mgr._classify_users = _classify_as_linked
    mgr._show_progress_card = _async_noop

    asyncio.run(mgr.initiate_ready_check(_stub_thread()))

    assert mgr.ready_discord_ids == {"0", "1"}
    assert mgr.ready_socket_ids == {"2"}
    assert mgr.expected_user_ids == {str(i) for i in range(7)}
    assert mgr.is_ready_check_held() is False


def test_a_socket_not_ready_marks_the_seat_without_ending_the_check():
    mgr, completed = _ready_manager([str(i) for i in range(4)])
    _stub_lobby_io(mgr)
    mgr._ready_check_started_at = -pod_draft_manager._READY_DEBOUNCE_S * 10

    asyncio.run(mgr._on_set_ready("2", "NotReady"))

    assert mgr.ready_check_active is True
    assert completed == []
    assert mgr.not_ready_ids == {"2"}
    assert "2" not in mgr.ready_users


def test_a_socket_not_ready_cannot_undo_an_answer_given_in_discord():
    mgr, _ = _ready_manager([str(i) for i in range(4)])
    _stub_lobby_io(mgr)
    mgr._ready_check_started_at = -pod_draft_manager._READY_DEBOUNCE_S * 10
    mgr.ready_discord_ids = {"2"}

    asyncio.run(mgr._on_set_ready("2", "NotReady"))

    assert mgr.ready_users == {str(i) for i in range(4)}
    assert mgr.not_ready_ids == set()


def test_the_echo_right_after_arming_is_ignored():
    mgr, _ = _ready_manager([str(i) for i in range(4)])
    _stub_lobby_io(mgr)
    mgr._ready_check_started_at = asyncio.new_event_loop().time()

    asyncio.run(mgr._on_set_ready("2", "NotReady"))

    assert mgr.not_ready_ids == set()
    assert mgr.ready_users == {str(i) for i in range(4)}


def test_an_answer_from_a_seat_outside_the_checked_roster_is_dropped():
    mgr, _ = _ready_manager([str(i) for i in range(4)])
    _stub_lobby_io(mgr)

    asyncio.run(mgr._on_set_ready("9", "Ready"))

    assert "9" not in mgr.ready_users


def test_a_seat_that_joined_late_is_taken_into_the_check_by_its_own_answer():
    """A pod never drafts a table nobody checked, and a player answering for themselves is that seat being
    checked. So the roster grows by one and the pause their arrival caused clears."""
    mgr, completed = _ready_manager([str(i) for i in range(4)])
    _stub_lobby_io(mgr)
    mgr.session_users = mgr.session_users + [{"userID": "9", "userName": "Latecomer#1"}]
    asyncio.run(mgr._on_session_users(mgr.session_users))
    assert mgr.ready_check_joined_ids == {"9"}

    assert asyncio.run(mgr.mark_seat("9", ready=True)) is None

    assert mgr.expected_user_ids == {"0", "1", "2", "3", "9"}
    assert mgr.is_ready_check_held() is False
    assert completed == [True]


def test_not_ready_from_discord_drops_the_seat_and_leaves_the_check_running():
    mgr, completed = _ready_manager([str(i) for i in range(4)])
    _stub_lobby_io(mgr)
    mgr.ready_discord_ids = {"2"}

    assert asyncio.run(mgr.mark_seat("2", ready=False)) is None

    assert mgr.ready_check_active is True
    assert completed == []
    assert "2" not in mgr.ready_users
    assert mgr.not_ready_ids == {"2"}

    assert asyncio.run(mgr.mark_seat("2", ready=True)) is None
    assert mgr.not_ready_ids == set()
    assert completed == [True]


def test_stopping_closes_the_check_but_keeps_what_players_answered():
    """A player who said they were ready is still ready. Both ways a check closes are about the pod, not
    about them, so the next check asks nobody to click twice."""
    mgr = _setup_manager([str(i) for i in range(8)])
    mgr.ready_check_active = True
    mgr.ready_discord_ids = {"0"}
    mgr.ready_socket_ids = {"1"}
    mgr.not_ready_ids = {"2"}

    assert asyncio.run(mgr.stop_ready_check(actor="Finkel")) is None

    assert mgr.ready_check_active is False
    assert mgr.ready_users == {"0", "1"}
    assert mgr.not_ready_ids == {"2"}
    assert mgr.last_cancel_reason


def test_a_resumed_check_carries_the_earlier_answers_and_drops_seats_that_left():
    mgr = _setup_manager([str(i) for i in range(8)])
    mgr.ready_check_active = True
    mgr.ready_discord_ids = {"0", "7"}
    mgr.not_ready_ids = {"1"}
    asyncio.run(mgr.stop_ready_check(actor="Finkel"))

    mgr.session_users = [{"userID": str(i), "userName": str(i)} for i in range(7)]
    mgr._classify_users = _classify_as_linked
    mgr._show_progress_card = _async_noop
    asyncio.run(mgr.initiate_ready_check(_stub_thread()))

    assert mgr.ready_discord_ids == {"0"}
    assert mgr.not_ready_ids == {"1"}
    assert mgr.last_cancel_reason is None


def test_stopping_a_check_that_is_not_running_reports_instead_of_closing():
    mgr = _setup_manager([str(i) for i in range(8)])
    mgr.ready_check_active = False

    assert asyncio.run(mgr.stop_ready_check(actor="Finkel")) is not None


def test_odd_and_short_rosters_need_confirmation_not_refusal():
    cases = [
        (8, None, False),
        (7, None, True),
        (4, None, True),
        (5, None, True),
        (4, 2, False),
        (3, 2, True),
    ]

    for seated, min_players, expected in cases:
        mgr, _ = _ready_manager([str(i) for i in range(seated)])
        mgr.ready_check_active = False
        mgr.sio = type("_Sio", (), {"connected": True})()

        assert mgr.ready_check_blocker() is None
        assert mgr.ready_check_needs_confirm([], min_players=min_players) is expected


def test_unrecognized_seats_need_confirmation_on_a_clean_roster():
    mgr, _ = _ready_manager([str(i) for i in range(8)])
    mgr.ready_check_active = False

    assert mgr.ready_check_needs_confirm([]) is False
    assert mgr.ready_check_needs_confirm(["Stranger#12345"]) is True


def test_a_running_check_no_longer_blocks_a_fresh_one():
    mgr, _ = _ready_manager([str(i) for i in range(8)])
    mgr.sio = type("_Sio", (), {"connected": True})()

    assert mgr.ready_check_blocker() is None


def test_a_full_lobby_arms_the_ready_check_nudge_on_pods_and_mocks():
    cases = [
        ("pod", 8, 0, True),
        ("pod", 7, 0, False),
        ("pod", 8, 1, False),
        ("mock", 8, 0, True),
        ("mock", 8, 3, True),
        ("mock", 7, 1, False),
    ]

    for kind, seated, unlinked, expected in cases:
        mgr, _ = _ready_manager([])
        mgr.kind = kind
        classified = [(f"p{i}", None if i < unlinked else f"P{i}") for i in range(seated)]

        assert mgr._lobby_pod_full(classified) is expected


def test_empty_lobby_is_still_a_hard_block():
    mgr, _ = _ready_manager([])
    mgr.ready_check_active = False
    mgr.sio = type("_Sio", (), {"connected": True})()

    assert mgr.ready_check_blocker() is not None


def test_roster_hold_detail_pluralizes_and_covers_both_directions():
    assert "`Ada`" in roster_hold_detail(["Ada"], [])
    assert "2 players" in roster_hold_detail([], ["Ada", "Bo"])

    both = roster_hold_detail(["Ada"], ["Bo"])
    assert "Ada" in both and "Bo" in both


def test_the_lobby_card_says_a_check_is_paused_without_retelling_it():
    """The pause detail and its hint live on the check's own card. A lobby card carrying them too printed the
    same paragraph twice, once under each of two nearly identical embeds."""
    stopped, _ = ready_status_banner("notready", cancel_reason="stopped", initiated_by="Alice#2")
    assert any("Alice#2" in line for line in stopped)

    held, color = ready_status_banner("held")
    assert len(held) == 1
    assert color == discord.Color.orange()


def test_confirm_text_covers_every_warning_at_once():
    text = ready_check_confirm_text(5, 6, ["Stranger#12345", "Wanderer#77"])

    assert "5" in text
    assert "Stranger#12345" in text
    assert "Wanderer#77" in text


def test_confirm_text_omits_warnings_that_do_not_apply():
    text = ready_check_confirm_text(8, 6, [])

    assert "Stranger" not in text
    assert len(text.splitlines()) == 1


async def _async_noop(*args, **kwargs):
    return None


async def _classify_as_linked(names):
    return [(name, name) for name in names]


def _stub_thread():
    return type("_Thread", (), {"send": None})()


def _setup_manager(user_ids: list[str]) -> PodDraftManager:
    """A manager ready to run initiate_ready_check with every network call stubbed out."""
    mgr, _ = _ready_manager(user_ids)
    mgr.ready_check_active = False
    mgr.ready_socket_ids = set()
    mgr.sio = type("_Sio", (), {"connected": True})()
    mgr._emit_with_ack = _async_noop
    mgr._refresh_lobby_status = _async_noop
    mgr._flip_progress_card_to_stopped = _async_noop
    mgr.refresh_lobby_now = _async_noop
    return mgr


def test_setup_abandoned_when_the_check_dies_mid_classification():
    mgr = _setup_manager([str(i) for i in range(8)])
    posted: list[bool] = []

    async def _stop_during_classification(names):
        await mgr._end_ready_check("stopped")
        return [(name, name) for name in names]

    async def _record_post(*args, **kwargs):
        posted.append(True)

    mgr._classify_users = _stop_during_classification
    mgr._show_progress_card = _record_post
    asyncio.run(mgr.initiate_ready_check(_stub_thread()))

    assert mgr.ready_check_active is False
    assert posted == []
    assert mgr.last_cancel_reason == "stopped"


def test_stale_timeout_leaves_the_next_check_alone(monkeypatch):
    monkeypatch.setattr(pod_draft_manager, "_READY_TIMEOUT_S", 0)
    mgr = _setup_manager([str(i) for i in range(8)])
    ended: list[str] = []

    async def _record(detail, *, timed_out=False):
        ended.append(detail)

    mgr._end_ready_check = _record
    mgr.ready_check_active = True
    mgr.ready_check_generation = 4

    asyncio.run(mgr._ready_timeout(3))

    assert ended == []


def test_timeout_fires_for_its_own_generation(monkeypatch):
    monkeypatch.setattr(pod_draft_manager, "_READY_TIMEOUT_S", 0)
    mgr = _setup_manager([str(i) for i in range(8)])
    ended: list[bool] = []

    async def _record(detail, *, timed_out=False):
        ended.append(timed_out)

    async def _no_post(*args, **kwargs):
        return None

    mgr._end_ready_check = _record
    mgr.bot = type("_Bot", (), {})()
    mgr._missing_names = lambda ids: ""
    mgr.ready_check_active = True
    mgr.ready_check_generation = 4
    pod_draft_manager.bot_log_mod.get = lambda bot: type("_Log", (), {"post": _no_post})()

    asyncio.run(mgr._ready_timeout(4))

    assert ended == [True]


def test_the_stopped_banner_survives_a_session_users_broadcast():
    mgr, _ = _ready_manager([str(i) for i in range(8)])
    mgr.ready_check_active = False
    mgr.last_cancel_reason = "stopped by Finkel"
    _stub_lobby_io(mgr)

    asyncio.run(mgr._on_session_users([{"userID": "1", "userName": "1"}]))

    assert mgr.last_cancel_reason == "stopped by Finkel"


def test_progress_card_renders_a_live_state_or_nothing_at_all():
    mgr, _ = _ready_manager([str(i) for i in range(8)])
    cases = [
        (True, "ready", "ready"),
        (True, "held", "held"),
        (False, "notready", "notready"),
        (False, "drafting", "drafting"),
        (False, "complete", "complete"),
        (False, "linked", None),
        (False, "unlinked", None),
        (False, "empty", None),
    ]

    for active, state, expected in cases:
        mgr.ready_check_active = active
        assert mgr._progress_card_state(state) == expected


def test_the_gap_between_closing_the_check_and_the_draft_reads_as_drafting():
    """`_start_draft` awaits a seating re-push and the startDraft ack with the check already closed. A lobby
    broadcast landing there used to render a pod with neither check nor draft, which the cards showed as a
    ready check somebody stopped."""
    mgr, _ = _ready_manager([str(i) for i in range(8)])
    mgr.ready_check_active = False
    classified = [(str(i), str(i)) for i in range(8)]

    mgr._draft_start_in_flight = True

    assert mgr._compute_state(classified) == "drafting"
    assert mgr._progress_card_state(mgr._compute_state(classified)) == "drafting"


def test_draftmancer_bots_are_seats_in_the_artifact_but_not_in_the_roster():
    mgr, _ = _ready_manager([])
    mgr.draft_logs = {"Finkel#1": {"users": {
        "u1": {"userName": "Finkel#1", "isBot": False},
        "u2": {"userName": "Bot #1", "isBot": True},
        "u3": {"userName": "LSV#2", "isBot": False},
        "u4": {"userName": "Bot #2", "isBot": True},
    }}}

    assert mgr._snapshot_tournament_roster() == ["Finkel#1", "LSV#2"]


def test_bots_pad_the_bench_table_and_never_a_community_pod(monkeypatch):
    """The fill is derived from the guild the bot runs in, so no env value can leave bots at a real pod's
    table. The seam stays in one place for a future mid-draft replacement of a player who drops."""
    mgr, _ = _ready_manager([str(i) for i in range(3)])
    mgr.max_players = 8

    monkeypatch.setattr(pod_draft_manager.settings, "production_guild_id", 775371722065051658)
    monkeypatch.setattr(pod_draft_manager.settings, "discord_guild_id", 775371722065051658)
    assert mgr._desired_bot_count() == 0

    monkeypatch.setattr(pod_draft_manager.settings, "discord_guild_id", 999)
    assert mgr._desired_bot_count() == 5

    mgr.session_users = [{"userID": str(i), "userName": str(i)} for i in range(9)]
    assert mgr._desired_bot_count() == 0


def test_lobby_state_reports_a_held_check_apart_from_a_running_one():
    mgr, _ = _ready_manager([str(i) for i in range(8)])
    classified = [(str(i), str(i)) for i in range(8)]

    assert mgr._compute_state(classified) == "ready"

    mgr.ready_check_left_ids = {"7"}
    assert mgr._compute_state(classified) == "held"
