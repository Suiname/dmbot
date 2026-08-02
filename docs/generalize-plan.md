# Generalization Plan

Goal: make dmbot runnable on any Discord server with minimal code changes. The original codebase was written for the LLU community and contains a mix of hardcoded server-specific values and configurable settings. This document tracks what needs to change, in what order, and what is intentionally deferred.

The primary target is **pod draft automation** — the Draftmancer integration, scheduling, signups, role management, and results tracking. The 17lands leaderboard feature is explicitly out of scope for now (see [Deferred: Leaderboard](#deferred-leaderboard) below).

---

## Scope

**In scope**
- Pod draft scheduling, signups, RSVPs, and role pings
- Draftmancer session creation and lobby management
- Match result reporting and pod standings
- Server guide (`!guide`) management
- Mock drafts
- Discord role and channel configuration
- All Discord commands that do not depend on 17lands or the public website

**Out of scope (this branch)**
- 17lands profile linking (`/join`, `/signout`, `/stats`, `/leaderboard`)
- Player stats and scoring (`bot/scoring.py`, `bot/services/refresh.py`, `bot/services/seventeenlands.py`)
- Public leaderboard website (React frontend + Supabase + Cloudflare Pages)
- Set awards ceremony
- P0P1 contest
- YouTube/podcast media sync
- Preview season awards

---

## ✅ Phase 1 — Configuration (no branding changes yet)

Get the bot running on a new server by filling in `.env`. No code changes, just setup.

- [x] Fork upstream, clone locally, create `generalize` branch
- [x] Create a Discord application at discord.com/developers
- [x] Get a bot token and invite the bot to the test server
- [x] Copy `.env.example` → `.env` and fill in all required fields
- [x] Run Postgres locally via Docker (`dmbot-pg` container, port 5433, using Podman Desktop)
- [x] Run `alembic upgrade head` to apply the schema
- [x] Run `python -m bot.scripts.seed_sets` to populate set data
- [x] Start the bot with `python -u -m bot.main`
- [x] Run `!sync` in the test server to push slash commands
- [x] Smoke test: confirmed `/help` appears and bot is posting to bot-log channel

**`.env` fields to fill in for a fresh server:**

```
DATABASE_URL=postgresql://postgres:devpw@localhost:5433/dischord
DISCORD_BOT_TOKEN=<your bot token>
DISCORD_GUILD_ID=<your test server ID>
DISCORD_ADMIN_ROLE_ID=<admin role ID>
DISCORD_BOTLOG_CHANNEL_ID=<a channel for bot logs>

# Override the LLU-specific defaults:
FEEDBACK_CHANNEL_ID=<your feedback channel ID>
POD_DRAFT_CHANNEL_ID=<your pod coordination channel ID>
POD_DRAFT_CHAT_CHANNEL_NAME=<your chat channel name>
POD_DRAFT_TROPHY_HYPE_CHANNEL_NAME=<your trophy channel name>
POD_DRAFT_VOICE_CHANNEL_NAME=<your voice channel name>
POD_DRAFT_SESSION_PREFIX=<short prefix, e.g. "MY">
PUBLIC_SITE_URL=<leave blank or point to a placeholder>
```

**Required Discord channels/roles to create on the test server** (matching your config values):
- A pod coordination forum channel (the `POD_DRAFT_CHANNEL_ID` target)
- A text channel matching `POD_DRAFT_CHAT_CHANNEL_NAME`
- A text channel matching `POD_DRAFT_TROPHY_HYPE_CHANNEL_NAME`
- A voice channel matching `POD_DRAFT_VOICE_CHANNEL_NAME`
- A channel for `DISCORD_BOTLOG_CHANNEL_ID`
- A channel for `FEEDBACK_CHANNEL_ID`
- Server Guide channels: `channel-overview`, `quick-links`, `rules`

---

## ✅ Phase 2 — Move `PRODUCTION_GUILD_ID` into config

**Why this matters:** `PRODUCTION_GUILD_ID` is a module-level constant set to the LLU server's ID. It is used in two places with real behavior impact:

1. `bot/commands/test_group.py` — guards `!test` commands so dangerous test states (ones that create real pods, signals, or roles) are blocked on the production server
2. `bot/services/pod_launch.py:1000` — checks whether to apply production-specific pod launch behavior

Until this is moved into config, the bot will silently behave as if it is not on the production server (because your guild ID won't match the hardcoded LLU ID). This is mostly harmless during development but wrong in production.

**Tasks:**
- [x] Add `production_guild_id: int | None = None` to `Settings` in `bot/config.py`, defaulting to `discord_guild_id` via `model_validator`
- [x] Remove `PRODUCTION_GUILD_ID = 775371722065051658` constant from `bot/config.py`
- [x] Update all import sites to use `settings.production_guild_id` instead:
  - `bot/commands/test_group.py`
  - `bot/commands/preview_season_awards.py`
  - `bot/services/pod_launch.py`
  - `bot/tests/test_test_group_guard.py` (monkeypatch settings; use neutral fixture ID)
  - `bot/tests/test_pod_rally.py` (replace LLU guild ID fixture with generic value)

---

## ✅ Phase 3 — Move remaining hardcoded IDs into config

One channel ID is still hardcoded outside of `Settings`:

- [x] `bot/commands/preview_season_awards.py` — `PREVIEW_SEASON_CHANNEL_ID = 775822803328040961`
  - Added `preview_season_channel_id: int | None = None` to `Settings`
  - Removed module-level constant; URL is now built at call time in `_msg_counted()`
  - Falls back to plain "preview-season" text when the setting is not configured

Note: `COMMUNITY_TZ = ZoneInfo("America/New_York")` in the same file is also hardcoded. Low-priority since it only affects the preview-season-awards command, but worth moving to config if that command will be used.

---

## Phase 4 — Community branding

Replace LLU-specific strings in user-facing copy with a configurable community name and URL. The cleanest approach is a single `community_name` setting (e.g. "My Server") and `public_site_url` (already in config).

**Add to `Settings`:**
```python
community_name: str = "this server"
```

**Files with LLU copy to update:**

| File | What to change |
|---|---|
| `bot/commands/signup.py:39` | "Welcome to the LLU Community Leaderboard!" |
| `bot/commands/delete_account.py:25,28` | "LLU leaderboard" (×2) |
| `bot/commands/guide.py:41` | `WEBHOOK_NAME = "LLU Server Guide"` |
| `bot/commands/save_resource.py:30` | `WEBHOOK_NAME = "LLU Resources"` |
| `bot/main.py:588` | Activity name `"limitedlevelups.com \| /join"` |
| `bot/services/mtgscribe.py:37` | User-Agent header |
| `bot/services/token_link_flow.py:35` | `LEADERBOARD_URL` hardcoded to limitedlevelups.com |
| `bot/services/championship_copy.py:57,107` | limitedlevelups.com URLs |
| `bot/commands/set_awards.py:52,53` | limitedlevelups.com URLs |
| `bot/listeners/auto_link_listener.py:40` | limitedlevelups.com URL |
| `bot/commands/testads.py:23` | Hardcoded discord.com/channels URL using LLU guild ID |
| `bot/services/media_sync.py:418` | Regex stripping "llu" and "limited level-ups" from titles |

**`llu` emoji references** — The `llu` emoji is used as a brand icon in ~15 places via `emojis.get("llu")` / `emojis.prefix("llu")`. These already degrade gracefully to empty string when the emoji is not uploaded. To use a custom icon: upload an app emoji named `llu` to your Discord application (or rename the references to a new name and upload under that name).

---

## Phase 5 — Server guide content

The five markdown files in `bot/server_guide/` are LLU-specific and need to be rewritten for the new community:

- [ ] `bot/server_guide/channel-overview.md` — channel directory
- [ ] `bot/server_guide/quick-links.md` — community links
- [ ] `bot/server_guide/rules.md` — server rules
- [ ] `bot/server_guide/limitedlevelups-com.md` — LLU website description (repurpose or remove)
- [ ] `bot/server_guide/dischord-bot.md` — bot intro (update community name and URLs)

The guide system itself (`!guide` command) is generic and needs no code changes — just new content.

---

## Phase 6 — Disable out-of-scope features

Rather than deleting the 17lands/leaderboard code (which would make pulling upstream changes harder), disable the features via config or by commenting out their setup calls in `bot/main.py`. This keeps the option to re-enable them later.

Features to disable in `bot/main.py` (comment out setup calls):
- `setup_signup` / `setup_signout` — 17lands profile linking
- `setup_link_17lands` — 17lands token linking
- `setup_leaderboard` / `setup_leaderboard_visibility` — leaderboard commands
- `setup_stats` — player stats
- `setup_set_awards` — set awards ceremony
- `setup_preview_season_awards` — preview season awards (or keep if wanted)
- `setup_event_scribe` — only relevant if using the 17lands event log
- Media sync tasks (`media_sync_enabled: bool = False` in config — already supported)
- Profile sync listener (`profile_sync_enabled: bool = False` in config — already supported)

---

## Deferred: Leaderboard

The 17lands leaderboard feature requires significant additional infrastructure. Documented here for future reference.

### What it is
Players link their [17lands](https://17lands.com) draft tracker profile to Discord. The bot pulls their draft history, computes a custom score (trophies × win rate × confidence factor), and ranks them on a public website by the current MTG set.

### Additional infrastructure required
- **Supabase project** — Postgres + public REST API (the anon key is read directly by the browser)
- **Cloudflare Pages** — hosting for the React frontend (`frontend/` directory)
- **Railway** (or any always-on host) — for the bot itself (already needed for the bot generally)
- **17lands API** — no key required, but bot makes HTTP requests to `17lands.com/data/draft`

### Code components
- `bot/services/seventeenlands.py` — HTTP client for 17lands
- `bot/services/refresh.py` — pulls drafts, writes `draft_events`, rebuilds `player_stats`
- `bot/scoring.py` — scoring formula (also implemented in `frontend/src/data/scoring.ts`)
- `bot/commands/signup.py`, `signout.py`, `stats.py`, `leaderboard.py` — Discord commands
- `frontend/` — React + Vite + TanStack Query SPA
- `supabase/` — Supabase config and `public_*` views

### Key decisions when implementing
- The scoring formula is implemented twice (Python + TypeScript) and must stay in sync
- The frontend reads from Supabase directly via anon key; no backend proxy
- Set rotation is date-driven (`bot/sets.py`) — no config change needed when a new MTG set releases
- Pod points (from pod drafts) feed into leaderboard scores — so the leaderboard and pod systems are coupled at the data layer even if decoupled in the UI
