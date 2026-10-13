# Live AI Agent — background worker

A small Python service (standard library only) that keeps `live_ai_agent_prompt.txt`
in sync with GitHub and bookkeeps the queue **without any AI**. It keeps running when
every AI agent is closed or out of credits. Agents add value on top of it; nothing
depends on them.

## What it does

| Every cycle | How |
|---|---|
| Sync with GitHub | commits a settled local edit (unchanged for 2 min), fetches, fast-forwards or rebases, pushes, then checks the remote really moved. A conflict aborts the rebase and waits — nothing is overwritten |
| Choose the mode | from `[QUOTA_STATUS]`: **normal** (every 60 s), **quota_wait** (sleeps until the verified `RESET_AT` + 1 min, no GitHub polling), **fallback_wait** (no verified reset: every 30 min, reason logged) |
| Bookkeep the queue (normal mode) | each due `READY` prompt occurrence is noted **once** in a local ledger and left `READY` for an agent; `PAUSE`/`RESUME`/`CANCEL` steers are applied by changing only `STATUS` and marking the steer `STEER_STATUS: STEER_DONE`, with a `STEER_RESULT` in `live_ai_agent_log.md`; `CHANGE` and `STATUS` steers are left for an agent |
| Publish its mode | `[WORKER_STATUS]` in the prompt file, written only when the mode changes |

It **never runs a command taken from the prompt file**: anyone who can push to the
repository would otherwise control this machine. An optional `dispatch_command`
(off by default; set only in the local `config.json`) can start an AI agent for a
due prompt, passing just the prompt id.

## Install

| Machine | Command |
|---|---|
| Windows (no admin needed) | `powershell -ExecutionPolicy Bypass -File worker\install_windows.ps1` |
| Linux (systemd user service) | `bash worker/install_linux.sh` |

Windows: a scheduled task `LiveAIAgentWorker` starts at logon and every 5 minutes as
a watchdog (a duplicate start is ignored; the worker's lock refuses a second copy).
Without a stored password it runs while the user is logged on, including a locked
screen. For logged-off operation, edit the task in Task Scheduler and choose
"Run whether user is logged on or not" (Windows asks for the password itself).

Remove with `install_windows.ps1 -Uninstall` or `install_linux.sh --uninstall`.

## Operate

```
python worker/live_ai_worker.py status     # mode, next wake, last sync, last error
python worker/live_ai_worker.py wake       # check now (also ends a quota wait early)
```

State, ledger and rotating logs (1 MB × 5): `%LOCALAPPDATA%\live_ai_agent\` on
Windows, `~/.local/state/live_ai_agent/` on Linux. Settings override in
`config.json` there, e.g. `{"poll_seconds": 60, "fallback_seconds": 1800}`.

## For an AI agent: the quota handoff

1. Read your quota only from the provider's own interface, API or usage logs.
2. By **85 % used** (15 % left) at the latest, fill `[QUOTA_STATUS]`: provider, quota
   type, observed time, status, remaining, reset time with offset, `RESET_VERIFIED: YES`
   only if the provider states it, evidence, last task, outstanding work.
3. Fetch first, change only that section, commit, push, and confirm the remote has it.
4. Unknown quota or reset: say `UNKNOWN` / `RESET_VERIFIED: NO`. The worker then uses
   its 30-minute fallback; it never invents a reset time.
5. Never rely on yourself to wake the worker at the reset — it schedules that itself.

After a verified reset passes, the worker resumes 60-second polling by policy
(`resume_after_reset`) and says the quota is not yet confirmed; the next agent
should update `[QUOTA_STATUS]` when it starts.

## Test

```
python -m pytest worker/test_live_ai_worker.py -q
```

The tests run against a throwaway bare repository standing in for GitHub: due-work
ledger, steers, a rejected push rebased and verified, a conflict that overwrites
nothing, a network failure with backoff and recovery, every quota mode, a simulated
reset that ends a wait, restart state, the single-instance lock, CRLF files.
