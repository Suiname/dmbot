import asyncio
from types import SimpleNamespace

import pytest

from bot.commands import test_group as group
from bot.config import settings

_PROD_ID = 111111111


class _Ctx(SimpleNamespace):
    async def send(self, *args, **kwargs):
        self.sent = True


def _ctx(guild_id: int | None) -> _Ctx:
    guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    return _Ctx(guild=guild, sent=False)


@pytest.mark.parametrize(
    "guild_id, name, expected",
    [
        (_PROD_ID, "launcher", True),
        (_PROD_ID, "poll", True),
        (_PROD_ID, "podswiss", True),
        (_PROD_ID, "rolling", False),
        (_PROD_ID, "reminders", False),
        (123456789, "launcher", False),
        (None, "launcher", False),
    ],
)
def test_production_refusal(monkeypatch, guild_id, name, expected):
    monkeypatch.setattr(settings, "production_guild_id", _PROD_ID)
    ctx = _ctx(guild_id)

    refused = asyncio.run(group.refused_on_production(ctx, name))

    assert refused is expected
    assert ctx.sent is expected
