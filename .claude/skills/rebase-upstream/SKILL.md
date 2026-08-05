---
name: rebase-upstream
description: Rebase dmbot's master onto upstream (MNoya/LimitedLevelUps) latest. Backs up master first, rebases, scans the newly-pulled commits for reintroduced LLU-specific hardcodes (production guild ID, preview season channel ID, disabled-feature defaults flipping back to True, stray Discord snowflake literals, LLU branding text), runs the full test suite, and fixes anything broken until it's green. Never pushes — hands off a clean local branch for the user to push manually.
---

# rebase-upstream

Rebases `master` onto `upstream/master` (MNoya/LimitedLevelUps), then verifies the fork's
generalization work (see `docs/generalize-plan.md`) wasn't clobbered by upstream changes.
Upstream still contains the original LLU-specific hardcodes this fork moved into
`Settings` / feature flags — a rebase can silently reintroduce them if upstream touches the
same files. This skill catches that before it becomes a prod incident.

Never push. Never skip failing tests to "just get it green." Never amend/force anything on
`master` other than the rebase itself. If you need to inspect the backup branch's contents
for comparison (e.g. to tell whether a failure predates the rebase), use `git worktree add
<path> backup/...` in a scratch directory — never `git checkout <backup-branch> -- .`, which
silently overwrites the working tree while HEAD stays on `master`.

## Workflow

### 1. Preflight

```
cd /Users/jt/Scripts/dmbot
git status
```

Working tree must be clean and on `master`. If there are uncommitted changes, stop and ask
the user how to handle them (stash vs. let them commit first) — do not stash or discard
silently.

```
git fetch upstream
git log --oneline master..upstream/master
```

If there's nothing new, tell the user master is already current and stop here.

### 2. Backup branch

```
git branch backup/master-pre-rebase-$(date +%Y%m%d-%H%M) master
```

Report the branch name to the user in the final summary — it's the rollback point if
anything here goes sideways.

### 3. Capture the pre-rebase scan baseline

Before rebasing, snapshot the current state of the four things upstream is prone to
reintroduce. Use a scratch dir so nothing lands in the repo:

```
SCAN=$(mktemp -d)
grep -rnE '^\s*PRODUCTION_GUILD_ID\s*=\s*[0-9]|^\s*PREVIEW_SEASON_CHANNEL_ID\s*=\s*[0-9]' bot/ --include=*.py | grep -v '/tests/' > "$SCAN/before_constants.txt"
grep -rnE '[0-9]{17,19}' bot/ --include=*.py | grep -v '/tests/' | grep -v 'bot/config.py' > "$SCAN/before_snowflakes.txt"
grep -rnE '\bLLU\b' bot/ --include=*.py --include=*.md | grep -v '/tests/' > "$SCAN/before_llu.txt"
grep -nE 'auto_refresh_enabled|leaderboard_enabled|media_sync_enabled|profile_sync_enabled' bot/config.py > "$SCAN/before_flags.txt"
echo "$SCAN"
```

Keep `$SCAN` around for step 6 — don't let it get cleaned up mid-skill.

### 4. Rebase

```
git rebase upstream/master
```

**If it completes clean**, move to step 5.

**If it conflicts**, resolve them. These files are the fork's known conflict-prone spots
(touched by the generalize work — see `docs/generalize-plan.md` phases 2, 3, 4, 6) and the
rule of thumb for each is: **keep the fork's settings/config-driven indirection, replay
upstream's new logic on top of it** — don't resolve a conflict by reverting to upstream's
literal constant.

| File | Keep from this fork |
|---|---|
| `bot/config.py` | `production_guild_id`, `preview_season_channel_id`, `community_name`, `podcast_title_prefix` fields; `leaderboard_enabled`/`auto_refresh_enabled`/`profile_sync_enabled`/`media_sync_enabled` all default `False` |
| `bot/commands/test_group.py` | `settings.production_guild_id` (not a hardcoded guild ID) |
| `bot/commands/preview_season_awards.py` | `settings.preview_season_channel_id`, `settings.production_guild_id`, no module-level ID constants |
| `bot/services/pod_launch.py` | `settings.production_guild_id` |
| `bot/commands/signup.py`, `bot/commands/delete_account.py` | `{community}` placeholder, not "LLU" literal copy |
| `bot/commands/guide.py`, `bot/commands/save_resource.py` | `WEBHOOK_NAME` derived from `settings.community_name`, not a literal string |
| `bot/main.py` | activity name from `settings.community_name` |
| `bot/services/mtgscribe.py` | User-Agent from `settings.community_name` / `settings.public_site_url` |
| `bot/services/token_link_flow.py`, `bot/listeners/auto_link_listener.py` | `settings.leaderboard_url`, not a literal URL |
| `bot/commands/testads.py` | `settings.production_guild_id` in the channel URL |
| `bot/services/media_sync.py` | `settings.podcast_title_prefix` |
| `bot/main.py` setup_hook | every `leaderboard_enabled`-gated `setup_*` call from Phase 6 stays gated |

For any conflict outside this table, take upstream's version unless it obviously
reintroduces an LLU-specific literal (community name, guild/channel ID, webhook name) —
in that case, wrap it the same way the table's pattern does.

After resolving each file: `git add <file>`, then `git rebase --continue`. Repeat until the
rebase finishes. If a conflict is genuinely ambiguous (unclear what upstream's new logic is
even doing), stop and ask the user rather than guessing.

### 5. Confirm the rebase landed

```
git status
git log --oneline backup/master-pre-rebase-*..master
```

Should show a clean tree and the newly-replayed commits.

### 6. Scan for reintroduced hardcodes

Re-run the same four scans against the post-rebase tree and diff against `$SCAN`:

```
grep -rnE '^\s*PRODUCTION_GUILD_ID\s*=\s*[0-9]|^\s*PREVIEW_SEASON_CHANNEL_ID\s*=\s*[0-9]' bot/ --include=*.py | grep -v '/tests/' > "$SCAN/after_constants.txt"
grep -rnE '[0-9]{17,19}' bot/ --include=*.py | grep -v '/tests/' | grep -v 'bot/config.py' > "$SCAN/after_snowflakes.txt"
grep -rnE '\bLLU\b' bot/ --include=*.py --include=*.md | grep -v '/tests/' > "$SCAN/after_llu.txt"
grep -nE 'auto_refresh_enabled|leaderboard_enabled|media_sync_enabled|profile_sync_enabled' bot/config.py > "$SCAN/after_flags.txt"

diff "$SCAN/before_constants.txt" "$SCAN/after_constants.txt"
diff "$SCAN/before_snowflakes.txt" "$SCAN/after_snowflakes.txt"
diff "$SCAN/before_llu.txt" "$SCAN/after_llu.txt"
diff "$SCAN/before_flags.txt" "$SCAN/after_flags.txt"
```

Only lines added (`>`) matter — those are new since the backup. For each:

- **New `PRODUCTION_GUILD_ID = <digits>` or `PREVIEW_SEASON_CHANNEL_ID = <digits>` constant**
  outside `bot/config.py`: this is Phase 2/3 regressing. Fix it the same way those phases
  did — remove the constant, use `settings.production_guild_id` /
  `settings.preview_season_channel_id` at the call site.
- **New 17–19 digit literal**: check what it is. A real reintroduced guild/channel ID used
  for behavior (not a comment, not a CDN attachment URL, not a test fixture) needs moving
  into `Settings` the way Phase 2/3 did. A harmless literal (e.g. a Discord CDN attachment
  path, a message-ID cache key) doesn't need changing — use judgment, but say what you
  decided and why in the final report.
- **New "LLU" text in `bot/**/*.py` or `bot/server_guide/*.md`**: user-facing copy
  reintroducing LLU branding needs the Phase 4 treatment (`{community}` /
  `settings.community_name` placeholder). Internal-only strings (log messages, variable
  names, code comments) aren't worth chasing — flag but don't necessarily change.
- **Any flag default in `bot/config.py` no longer reading `False`**: this is Phase 6
  regressing and always needs fixing — flip it back to `False` and re-apply
  `settings.<flag>`-gating to whatever new upstream code doesn't yet check it (look for new
  `setup_*` calls in `bot/main.py`'s `setup_hook` that Phase 6's table doesn't already
  cover, per `docs/generalize-plan.md`).

Fix everything that needs fixing before moving on. If unsure whether something is a real
regression or a false positive, ask the user rather than guessing either direction.

### 7. Run the full verification suite

```
docker ps --filter name=dmbot-pg --format "{{.Names}}"
```

Start it if not running: `docker start dmbot-pg`.

```
DATABASE_URL=postgresql://postgres:devpw@localhost:5433/dmbot .venv/bin/alembic check
TEST_DATABASE_URL=postgresql://postgres:devpw@localhost:5433/dmbot_test .venv/bin/pytest bot/tests/
```

**Always pass `DATABASE_URL` explicitly for `alembic check`.** `.env`'s `DATABASE_URL` points
at the live production Supabase database — running the bare command silently checks drift
against prod instead of local dev. It's a read-only check either way, but the result is
meaningless (prod has migrations/history local dev doesn't) and it's not something to touch
outside this one explicit, deliberate override.

`alembic check` catches model/migration drift from any new upstream migrations. If it
fails, resolve the drift (new migration needed, or a hand-merged migration conflict from
step 4) before touching tests.

### 8. Fix failures until green

For each pytest failure, read the actual failure, not just the name — it may be:
- A real bug in newly-rebased upstream code
- A test that assumed the old hardcoded value and needs updating to the fork's config-driven
  equivalent (check `bot/tests/test_test_group_guard.py`, `bot/tests/test_pod_rally.py` —
  these were already adapted once in Phase 2, a rebase can reintroduce the old fixture)
- Fallout from a conflict resolution in step 4 that needs another pass

Iterate: fix, rerun `pytest bot/tests/`, repeat until 100% pass. Don't mark anything
`xfail`/`skip` to force green — if something can't be fixed, stop and tell the user why
instead of hiding it.

### 9. Report

Tell the user, concisely:
- Backup branch name (the rollback point)
- How many commits were pulled in from upstream (`git log --oneline backup/...​..master | wc -l`)
- What the scan found, if anything, and what was changed to fix it (file:line level)
- Final test status (must be 100% passing — this skill doesn't hand off a red branch)
- Whether `!sync` is needed (any command name/description changed in the pulled commits —
  check `git log -p backup/master-pre-rebase-*..master -- bot/commands/` for
  `@app_commands.command(` / `description=` changes)
- That master is ready to push, and that pushing itself is the user's call (per this repo's
  convention, Claude never pushes)
