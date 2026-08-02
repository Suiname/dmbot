"""Owner-only `!test <word>` prefix group.

Manual test triggers register here as subcommands and reuse the production builders they
exercise, so test output can't drift from what the real flow sends. Words that match no
subcommand fall through to the testlobby state handler when it's registered.
invoke_without_command=True means group checks don't run for subcommands — each
subcommand carries its own @commands.is_owner().

A subcommand runs on the production guild only when PRODUCTION_SAFE_TESTS names it, so a
surface that creates real pods, signals, or roles can only be driven in the test server.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from discord.ext import commands

from bot.config import settings


HALL_OF_FAME = (
    "Finkel", "LSV", "The Hump", "Paolo", "Shota", "Reid", "Chapin", "JED",
    "Nassif", "Huey", "Kibler", "Levy", "Nakamura", "Karsten", "Juza", "Owen",
    "Budde", "Kastle", "Maher", "Rietzl", "Zvi", "Pikula", "Ruel", "Herberholz",
    "Rubin", "Wafo", "Comer", "Baker", "Watanabe", "Mihara", "Sadin", "Duke",
    "Yurchick", "Tsumura",
)

PRODUCTION_SAFE_TESTS = frozenset({
    "ads", "awards", "cardformat", "champcard", "component", "deckping", "firenudge", "formatschedule",
    "lifecycle", "lockroster", "mockcard", "myset", "named", "overflow", "pollnudge", "queueclosed",
    "p2vote", "rally", "reminder", "reminders", "rolegrant", "rolling", "scribe", "sendoff", "setawards",
    "teamcard",
    "thread-intro", "tiebreakers", "underfill", "welcome", "widths",
})

MSG_TEST_PRODUCTION_BLOCKED = (
    "`!test {name}` creates real pods, signals, or roles, so it is disabled on the production guild. "
    "Run it in the test server."
)

TestFallback = Callable[[commands.Context, str, str], Awaitable[None]]

_fallback: TestFallback | None = None


def register_test_fallback(handler: TestFallback) -> None:
    global _fallback
    _fallback = handler


@commands.group(name="test", invoke_without_command=True)
@commands.is_owner()
async def test_group(ctx: commands.Context, state: str = "", extra: str = "") -> None:
    if _fallback is not None:
        await _fallback(ctx, state, extra)
        return
    names = ", ".join(sorted(f"`!test {command.name}`" for command in test_group.commands))
    await ctx.send(f"Available tests: {names}")


async def refused_on_production(ctx: commands.Context, name: str) -> bool:
    """Whether this `!test` surface must not run here, having sent the refusal when so. A surface that
    writes real pods, signals, or roles is indistinguishable from a real one once it lands in the
    community server, so the production guild allows only the render-only previews named above. The
    allowlist is opt-in: a test surface added later is refused there until it is listed.

    Also called with a state word by the testlobby fallback, whose live states seed real pods."""
    if ctx.guild is None or settings.production_guild_id is None or ctx.guild.id != settings.production_guild_id or name in PRODUCTION_SAFE_TESTS:
        return False
    await ctx.send(MSG_TEST_PRODUCTION_BLOCKED.format(name=name))
    return True


async def setup(bot: commands.Bot) -> None:
    bot.add_command(test_group)
    bot.add_check(_production_guard)


async def _production_guard(ctx: commands.Context) -> bool:
    """Global check so every `!test` subcommand is covered without each one carrying the guard, and a new
    subcommand inherits it. The bare group falls through: its state word is guarded in testlobby, which
    is the only place the word is parsed."""
    if ctx.command is None or ctx.command.root_parent is not test_group:
        return True
    return not await refused_on_production(ctx, ctx.command.name)
