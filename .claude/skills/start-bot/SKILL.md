---
name: start-bot
description: Start the dmbot Discord bot in a background shell. Checks that the Podman/Docker container is running and starts the Python process in the background, then tails the log briefly to confirm startup.
---

# start-bot

Start the dmbot Discord bot in the background.

## Workflow

### 1. Check Postgres container

Run:
```
docker ps --filter name=dmbot-pg --format "{{.Names}}"
```

If `dmbot-pg` is not listed, start it:
```
docker start dmbot-pg
```

Wait a moment and confirm it started (run `docker ps` again). If it still fails, stop and report the error.

### 2. Start the bot in the background

Run the bot with `run_in_background: true`:
```
.venv/bin/python -u -m bot.main
```

The working directory must be `/Users/jt/Scripts/dmbot` (the project root). The bot reads `.env` automatically via pydantic-settings.

### 3. Confirm startup

The bot logs to `logs/bot.log`. Tail a few lines after a short pause to confirm it started cleanly:
```
sleep 3 && tail -20 logs/bot.log
```

Look for lines indicating the bot connected to Discord (e.g. `Logged in as`, `Bot is ready`). If you see an error traceback instead, report it verbatim.

### 4. Report

Tell the user:
- The bot is running in the background
- Reminder: run `!sync` in Discord if any slash command names/descriptions changed since last run (body-only changes don't need it)
- How to tail the log live: `tail -f logs/bot.log`
- How to stop the bot: find the PID with `pgrep -f "bot.main"` and kill it

## Notes

- The bot process is unmanaged — it will die if the terminal session ends. For a persistent background process, the user would need a process manager (e.g. `launchd`, `pm2`, or `nohup`).
- `!sync` must be run as a DM to the bot by the bot owner after any command schema change.
- The container name is `dmbot-pg` (not the original `dischord-pg`).
