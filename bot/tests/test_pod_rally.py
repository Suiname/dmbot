import asyncio
from dataclasses import dataclass, field

import pytest

from bot.services import pod_rally
from bot.services.pod_active import ACTIVE_POD_MANAGERS, ACTIVE_TABLE_VIEWS
from bot.services.pod_rally import (
    KIND_GATHERING,
    KIND_LOBBY,
    KIND_STARTED,
    KIND_TABLE,
    RallyTarget,
    forming_table_targets,
    live_lobby_targets,
    lobby_is_full,
    resolve_target,
)


GUILD_ID = "123456789"


@dataclass
class FakeManager:
    event_name: str
    thread_id: int
    session_id: str
    seated: int
    kind: str = "tournament"
    drafting: bool = False
    draft_complete: bool = False
    event_id: str = "event"

    def player_session_users(self) -> list[dict]:
        return [{"userID": f"u{i}"} for i in range(self.seated)]


@dataclass
class FakeClaimMessage:
    jump_url: str = "https://discord.com/channels/g/c/m"


@dataclass
class FakeTableView:
    table_name: str
    claims: dict[int, str]
    materialized: bool = False
    superseded: bool = False
    claim_message: FakeClaimMessage | None = field(default_factory=FakeClaimMessage)


def claims(count: int) -> dict[int, str]:
    return {index: f"player{index}" for index in range(count)}


@pytest.fixture
def registry():
    ACTIVE_POD_MANAGERS.clear()
    yield ACTIVE_POD_MANAGERS
    ACTIVE_POD_MANAGERS.clear()


@pytest.fixture
def tables():
    ACTIVE_TABLE_VIEWS.clear()
    yield ACTIVE_TABLE_VIEWS
    ACTIVE_TABLE_VIEWS.clear()


@pytest.fixture
def no_gathering(monkeypatch):
    monkeypatch.setattr(pod_rally, "gathering_targets_sync", lambda guild_id: [])


@pytest.mark.parametrize("seated,expected", [
    (0, False),
    (7, False),
    (8, True),
    (9, True),
])
def test_lobby_is_full_at_the_target_and_above(seated, expected):
    target = RallyTarget(KIND_LOBBY, "Late Pod", "url", seated=seated, session_id="s")

    assert lobby_is_full(target) is expected


def test_a_running_draft_is_never_reported_as_a_full_lobby():
    target = RallyTarget(KIND_STARTED, "Late Pod", "url", seated=8)

    assert lobby_is_full(target) is False


def test_live_lobby_targets_leads_with_the_pod_closest_to_a_full_table(registry):
    registry["a"] = FakeManager("Early Pod", 1, "sess-a", seated=4)
    registry["b"] = FakeManager("Late Pod", 2, "sess-b", seated=7)

    targets = live_lobby_targets(GUILD_ID)

    assert [target.name for target in targets] == ["Late Pod", "Early Pod"]


def test_live_lobby_targets_sinks_a_full_lobby_below_one_still_short(registry):
    registry["a"] = FakeManager("Full Pod", 1, "sess-a", seated=8)
    registry["b"] = FakeManager("Short Pod", 2, "sess-b", seated=6)

    targets = live_lobby_targets(GUILD_ID)

    assert [target.name for target in targets] == ["Short Pod", "Full Pod"]


def test_live_lobby_targets_sinks_a_running_draft_below_every_open_lobby(registry):
    registry["a"] = FakeManager("Running Pod", 1, "sess-a", seated=8, drafting=True)
    registry["b"] = FakeManager("Open Pod", 2, "sess-b", seated=3)

    targets = live_lobby_targets(GUILD_ID)

    assert [(target.name, target.kind) for target in targets] == [
        ("Open Pod", KIND_LOBBY), ("Running Pod", KIND_STARTED),
    ]


def test_live_lobby_targets_drops_mocks_and_completed_drafts(registry):
    registry["a"] = FakeManager("Mock Draft", 1, "sess-a", seated=5, kind="mock")
    registry["b"] = FakeManager("Done Pod", 2, "sess-b", seated=8, draft_complete=True)
    registry["c"] = FakeManager("Open Pod", 3, "sess-c", seated=5)

    targets = live_lobby_targets(GUILD_ID)

    assert [target.name for target in targets] == ["Open Pod"]


def test_live_lobby_targets_carries_the_session_id_only_for_joinable_lobbies(registry):
    registry["a"] = FakeManager("Open Pod", 1, "sess-a", seated=5)
    registry["b"] = FakeManager("Running Pod", 2, "sess-b", seated=8, drafting=True)

    by_name = {target.name: target for target in live_lobby_targets(GUILD_ID)}

    assert by_name["Open Pod"].session_id == "sess-a"
    assert by_name["Running Pod"].session_id is None


def test_forming_table_targets_leads_with_the_table_closest_to_firing(tables):
    tables["a"] = FakeTableView("Early Pod - Table 2", claims(1))
    tables["b"] = FakeTableView("Late Pod - Table 2", claims(3))

    targets = forming_table_targets()

    assert [target.name for target in targets] == ["Late Pod - Table 2", "Early Pod - Table 2"]


def test_forming_table_targets_drops_tables_that_can_no_longer_be_joined(tables):
    tables["a"] = FakeTableView("Fired Table", claims(4), materialized=True)
    tables["b"] = FakeTableView("Replaced Table", claims(2), superseded=True)
    tables["c"] = FakeTableView("Unposted Table", claims(2), claim_message=None)
    tables["d"] = FakeTableView("Open Table", claims(2))

    targets = forming_table_targets()

    assert [target.name for target in targets] == ["Open Table"]


def test_a_full_lobby_does_not_hide_a_second_table_still_gathering(registry, tables, no_gathering):
    registry["a"] = FakeManager("Full Pod", 1, "sess-a", seated=8)
    tables["a"] = FakeTableView("Full Pod - Table 2", claims(2))

    target = asyncio.run(resolve_target(GUILD_ID))

    assert (target.kind, target.name) == (KIND_TABLE, "Full Pod - Table 2")


def test_a_running_draft_does_not_hide_a_pod_gathering_for_later(registry, tables, monkeypatch):
    registry["a"] = FakeManager("Running Pod", 1, "sess-a", seated=8, drafting=True)
    later = RallyTarget(KIND_GATHERING, "Late Pod", "url", yes=3)
    monkeypatch.setattr(pod_rally, "gathering_targets_sync", lambda guild_id: [later])

    target = asyncio.run(resolve_target(GUILD_ID))

    assert target is later


def test_a_lobby_with_seats_left_outranks_a_second_table(registry, tables, no_gathering):
    registry["a"] = FakeManager("Open Pod", 1, "sess-a", seated=5)
    tables["a"] = FakeTableView("Other Pod - Table 2", claims(3))

    target = asyncio.run(resolve_target(GUILD_ID))

    assert (target.kind, target.name) == (KIND_LOBBY, "Open Pod")


def test_a_full_lobby_is_still_reported_when_nothing_else_is_recruiting(registry, tables, no_gathering):
    registry["a"] = FakeManager("Full Pod", 1, "sess-a", seated=8)

    target = asyncio.run(resolve_target(GUILD_ID))

    assert lobby_is_full(target) is True
