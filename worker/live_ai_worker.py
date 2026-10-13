#!/usr/bin/env python3
"""Live AI Agent background worker: keeps the prompt queue moving with no AI involved.

    python live_ai_worker.py run   --repo PATH     # the service (Task Scheduler / systemd)
    python live_ai_worker.py once  --repo PATH     # one cycle, then exit
    python live_ai_worker.py status                # local state, read-only
    python live_ai_worker.py wake                  # ask a running worker to check now

What it does, every cycle, using only git and the standard library:

1. Sync `live_ai_agent_prompt.txt` with GitHub: commit a settled local edit,
   fetch, fast-forward or rebase, push, and verify the remote really moved.
   A conflict is never resolved by overwriting: the rebase is aborted and the
   worker waits for a person or an agent.
2. Read `[QUOTA_STATUS]` and choose its mode: `normal` (poll every 60 s),
   `quota_wait` (sleep until the verified reset, no GitHub polling), or
   `fallback_wait` (no verified reset: a conservative interval, reason logged).
3. In `normal` mode, bookkeep the queue: note each due PROMPT occurrence once
   in a local ledger (an AI task stays pending for an agent), apply the
   deterministic steers (PAUSE, RESUME, CANCEL) by changing only STATUS, and
   leave CHANGE and STATUS steers for an agent.

It never runs a command named in the prompt file: text pulled from a shared
repository is data, and running it would hand the machine to anyone who can
push. An optional `dispatch_command` (off by default, set only in the local
config) can start an AI agent for due prompts; the prompt id is passed as an
argument and nothing from the file reaches a shell.

State, ledger and rotating logs live outside the repository:
`%LOCALAPPDATA%\\live_ai_agent` on Windows, `~/.local/state/live_ai_agent`
elsewhere (override with `--state-dir`). No credential is stored anywhere by
this worker; git uses the machine's own credential manager.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROMPT_FILE = "live_ai_agent_prompt.txt"
LOG_FILE = "live_ai_agent_log.md"

DEFAULTS: dict[str, Any] = {
    "remote": "origin",
    "branch": "main",
    "timezone": "Asia/Kolkata",
    "poll_seconds": 60,               # normal mode
    "fallback_seconds": 1800,         # quota limited, reset unknown or unverified
    "reset_grace_seconds": 60,        # wake a little after the reset, not before
    "resume_after_reset": True,       # policy: a passed verified reset resumes polling
    "local_edit_quiet_seconds": 120,  # a local edit is committed only once it settles
    "git_timeout_seconds": 60,
    "max_backoff_seconds": 900,
    "push_attempts": 3,
    "heartbeat_seconds": 3600,        # a waiting worker logs that it is alive this often
    "dispatch_command": None,         # e.g. ["claude", "-p"]; off unless set locally
}

LIMITED = {"NEAR_LIMIT", "EXHAUSTED", "UNAVAILABLE"}
NEVER_AUTO = {"TEMPLATE", "DONE", "PAUSED", "CANCELLED", "BLOCKED", "FAILED"}
log = logging.getLogger("live_ai_worker")


# ------------------------------------------------------------------ time

def tz_for(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                 # Windows without tzdata: IST has no DST
        if name == "Asia/Kolkata":
            return timezone(timedelta(hours=5, minutes=30), "IST")
        return timezone.utc


def parse_when(value: str, tz) -> datetime | None:
    """RUN_AT / RESET_AT: an aware ISO instant, or a date (09:00 that day). None if absent."""
    value = (value or "").strip()
    if not value or value.upper() in {"MANUAL_ONLY", "UNKNOWN", "NONE", "-"}:
        return None
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, 9, 0, tzinfo=tz)
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


# ------------------------------------------------------------------ the file

@dataclass
class Block:
    kind: str                         # PROMPT | STEER
    start: int                        # line index of the "[ PROMPT ]" marker
    end: int                          # line index of END_PROMPT / END_STEER
    fields: dict[str, str] = field(default_factory=dict)
    field_lines: dict[str, int] = field(default_factory=dict)


def _section(lines: list[str], name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    inside = False
    for line in lines:
        s = line.strip()
        if s == f"[{name}]":
            inside = True
        elif s == f"[END_{name}]":
            break
        elif inside and ":" in s and not s.startswith("#"):
            k, v = s.split(":", 1)
            out[k.strip().upper()] = v.strip()
    return out


def parse(text: str) -> tuple[list[Block], dict[str, str], list[str]]:
    """Live (uncommented) PROMPT and STEER blocks, the quota section, and the lines."""
    lines = text.splitlines()
    blocks: list[Block] = []
    current: Block | None = None
    in_text = False
    for i, line in enumerate(lines):
        s = line.strip()
        if current is None:
            if s in ("[ PROMPT ]", "[ STEER ]"):
                current = Block(kind=s.strip("[] "), start=i, end=-1)
                in_text = False
            continue
        if s == f"END_{current.kind}":
            current.end = i
            blocks.append(current)
            current = None
            continue
        if in_text:
            continue
        if current.kind == "PROMPT" and s.upper().startswith("PROMPT_TEXT:"):
            in_text = True
            continue
        m = re.match(r"^([A-Z_]+):\s*(.*)$", s)
        if m:
            current.fields[m.group(1)] = m.group(2).strip()
            current.field_lines[m.group(1)] = i
    return blocks, _section(lines, "QUOTA_STATUS"), lines


def set_field(edits: list[tuple[int, int, str]], block: Block, key: str, value: str) -> None:
    """Plan one header-field change; add the field before the block's end if absent."""
    if key in block.field_lines:
        edits.append((block.field_lines[key], 1, f"{key}: {value}"))
    else:
        edits.append((block.end, 0, f"{key}: {value}"))


def apply_edits(lines: list[str], edits: list[tuple[int, int, str]]) -> list[str]:
    """`(line, replace?, text)` applied bottom-up, so an insert never shifts a later edit."""
    out = list(lines)
    for i, replace, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if replace:
            indent = out[i][: len(out[i]) - len(out[i].lstrip())]
            out[i] = indent + text
        else:
            out.insert(i, text)
    return out


def set_section(lines: list[str], name: str, values: dict[str, str], note: str) -> list[str]:
    body = [f"[{name}]", *(f"{k}: {v}" for k, v in values.items()), f"[END_{name}]"]
    try:
        a = next(i for i, l in enumerate(lines) if l.strip() == f"[{name}]")
        b = next(i for i, l in enumerate(lines) if l.strip() == f"[END_{name}]")
        return lines[:a] + body + lines[b + 1:]
    except StopIteration:
        marker = next((i for i, l in enumerate(lines) if "[ HOW TO ADD A TASK ]" in l), len(lines))
        return lines[:marker] + [note, *body, "", ""] + lines[marker:]


# ------------------------------------------------------------------ git

class GitError(RuntimeError):
    pass


class Repo:
    def __init__(self, path: Path, cfg: dict):
        self.path, self.cfg = path, cfg
        self.remote, self.branch = cfg["remote"], cfg["branch"]

    def git(self, *args: str, check: bool = True) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        try:
            done = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                                  text=True, timeout=self.cfg["git_timeout_seconds"], env=env,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if check and done.returncode:
            raise GitError(f"git {' '.join(args[:2])}: {(done.stderr or done.stdout).strip()[:300]}")
        return done.stdout.strip()

    @property
    def upstream(self) -> str:
        return f"{self.remote}/{self.branch}"

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def dirty(self, name: str) -> bool:
        return bool(self.git("status", "--porcelain", "--", name))

    def counts(self) -> tuple[int, int]:
        ahead, behind = self.git("rev-list", "--left-right", "--count",
                                 f"HEAD...{self.upstream}").split()
        return int(ahead), int(behind)

    def remote_head(self) -> str:
        out = self.git("ls-remote", self.remote, f"refs/heads/{self.branch}")
        return out.split()[0] if out else ""

    def push_verified(self) -> str:
        """Push HEAD, rebasing on a rejection; return the verified remote head."""
        for attempt in range(1, self.cfg["push_attempts"] + 1):
            try:
                self.git("push", self.remote, f"HEAD:{self.branch}")
            except GitError as exc:
                log.warning("push rejected (attempt %d): %s", attempt, exc)
                self.git("fetch", self.remote, self.branch)
                self.rebase()
                continue
            remote = self.remote_head()
            if remote != self.head():
                raise GitError(f"pushed, but the remote is at {remote[:10]}, not {self.head()[:10]}")
            return remote
        raise GitError(f"push failed after {self.cfg['push_attempts']} attempts")

    def rebase(self) -> None:
        try:
            self.git("rebase", self.upstream)
        except GitError as exc:
            self.git("rebase", "--abort", check=False)
            raise GitError(f"conflict with {self.upstream}; nothing overwritten, waiting for a "
                           f"person or agent to reconcile: {exc}") from exc


def sync(repo: Repo, cfg: dict, now: float) -> str:
    """Bring the local copy and GitHub together. Returns the prompt file's text."""
    path = repo.path / PROMPT_FILE
    if repo.dirty(PROMPT_FILE):
        if now - path.stat().st_mtime >= cfg["local_edit_quiet_seconds"]:
            repo.git("add", "--", PROMPT_FILE)
            repo.git("commit", "-m", f"Local edit of the prompt queue ({socket.gethostname()})")
            log.info("committed a settled local edit")
        else:
            log.info("a local edit is still settling; not syncing over it this cycle")
            repo.git("fetch", repo.remote, repo.branch)
            return path.read_text(encoding="utf-8")
    repo.git("fetch", repo.remote, repo.branch)
    ahead, behind = repo.counts()
    if behind and not ahead:
        repo.git("merge", "--ff-only", repo.upstream)
    elif ahead:
        if behind:
            repo.rebase()
        repo.push_verified()
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ state

class State:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.ledger_path = root / "ledger.jsonl"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.ledger: set[str] = set()
        if self.ledger_path.exists():
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                try:
                    self.ledger.add(json.loads(line)["key"])
                except (ValueError, KeyError):
                    continue

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(self, key: str, event: str, **detail: Any) -> bool:
        """Append once per key; False when this occurrence was already handled."""
        if key in self.ledger:
            return False
        self.ledger.add(key)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "event": event, "at": iso_now(), **detail}) + "\n")
        return True


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ decisions

def choose_mode(quota: dict[str, str], cfg: dict, now: datetime, tz) -> tuple[str, datetime, str]:
    """(mode, next wake, reason) from the quota section alone."""
    status = quota.get("STATUS", "UNKNOWN").upper()
    poll = now + timedelta(seconds=cfg["poll_seconds"])
    if status not in LIMITED:
        return "normal", poll, f"quota {status.lower()}"
    reset = parse_when(quota.get("RESET_AT", ""), tz)
    verified = quota.get("RESET_VERIFIED", "").upper() in {"YES", "TRUE"}
    fallback = now + timedelta(seconds=cfg["fallback_seconds"])
    if reset is None or not verified:
        return "fallback_wait", fallback, (f"quota {status.lower()}; reset time "
                                           f"{'missing' if reset is None else 'unverified'}")
    if reset > now:
        return "quota_wait", reset + timedelta(seconds=cfg["reset_grace_seconds"]), \
            f"quota {status.lower()} until the verified reset {reset.isoformat()}"
    if cfg["resume_after_reset"]:
        return "normal", poll, (f"verified reset {reset.isoformat()} has passed; polling "
                                "resumed by policy — quota not yet confirmed by an agent")
    return "fallback_wait", fallback, "reset passed but policy waits for an agent to confirm"


def process(blocks: list[Block], lines: list[str], state: State, now: datetime, tz,
            cfg: dict) -> tuple[list[str], list[str]]:
    """Bookkeep the queue. Returns the edited lines and a STEER_RESULT per applied steer."""
    edits: list[tuple[int, int, str]] = []
    prompts = {b.fields.get("PROMPT_ID", ""): b for b in blocks if b.kind == "PROMPT"}
    results: list[str] = []
    due = []
    for pid, b in prompts.items():
        status = b.fields.get("STATUS", "").upper()
        when = parse_when(b.fields.get("RUN_AT", ""), tz)
        if not pid or status != "READY" or when is None or when > now:
            continue
        due.append((when, {"HIGH": 0, "NORMAL": 1, "LOW": 2}.get(
            b.fields.get("PRIORITY", "NORMAL").upper(), 1), b.start, pid, when))
    for _w, _p, _s, pid, when in sorted(due):
        key = f"{pid}@{when.isoformat()}"
        if state.record(key, "due_pending_agent", title=prompts[pid].fields.get("TITLE", "")):
            log.info("due: %s (needs an AI agent; left READY for one)", key)
            state.data["last_processed"] = key
            dispatch(cfg, pid, key)
    for b in (b for b in blocks if b.kind == "STEER"):
        if b.fields.get("STEER_STATUS", "").upper() == "STEER_DONE":
            continue
        target, action = b.fields.get("TARGET", "").strip(), b.fields.get("ACTION", "").upper()
        key = f"steer@{b.start}:{target}:{action}:{b.fields.get('NOTE', '')}"
        if action not in {"PAUSE", "RESUME", "CANCEL"}:
            if state.record(key, "steer_pending_agent"):
                log.info("steer %s %s needs an AI agent; left pending", action, target)
            continue
        hits = list(prompts.values()) if target.upper() == "ALL" else (
            [prompts[target]] if target in prompts else [])
        changed = []
        for p in hits:
            old = p.fields.get("STATUS", "").upper()
            new = {"PAUSE": "PAUSED", "CANCEL": "CANCELLED", "RESUME": "READY"}[action]
            if (action == "RESUME" and old != "PAUSED") or old in {"DONE", "CANCELLED"} \
                    or old == new or old == "TEMPLATE":
                continue
            set_field(edits, p, "STATUS", new)
            changed.append(f"{p.fields.get('PROMPT_ID')}: {old} -> {new}")
        set_field(edits, b, "STEER_STATUS", "STEER_DONE")
        outcome = "APPLIED" if hits else "IGNORED"
        results.append("\n".join([
            "STEER_RESULT", f"TARGET:     {target}", f"ACTION:     {action}",
            f"OUTCOME:    {outcome}",
            f"OUTPUT:     {'; '.join(changed) if changed else 'no status changed'}",
            f"NOTES:      {'target not found' if not hits else 'applied by the background worker'}",
            f"AT:         {now.isoformat(timespec='seconds')}"]))
        state.record(key, "steer_applied", outcome=outcome)
    return apply_edits(lines, edits), results


def dispatch(cfg: dict, pid: str, key: str) -> None:
    command = cfg.get("dispatch_command")
    if not command:
        return
    try:
        subprocess.Popen([*command, pid], cwd=cfg["_repo"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("dispatched %s to %s", key, command[0])
    except OSError as exc:
        log.warning("dispatch of %s failed: %s", key, exc)


# ------------------------------------------------------------------ the loop

class Worker:
    def __init__(self, repo_dir: Path, state_dir: Path, cfg: dict):
        self.cfg = {**cfg, "_repo": str(repo_dir)}
        self.repo = Repo(repo_dir, self.cfg)
        self.state = State(state_dir)
        self.tz = tz_for(cfg["timezone"])
        self.backoff = 0

    def write(self, lines: list[str], message: str, results: list[str]) -> None:
        target = self.repo.path / PROMPT_FILE
        eol = "\r\n" if b"\r\n" in target.read_bytes() else "\n"
        target.write_bytes((eol.join(lines) + eol).encode("utf-8"))
        names = [PROMPT_FILE]
        if results:
            path = self.repo.path / LOG_FILE
            old = path.read_text(encoding="utf-8") if path.exists() else "# Execution log\n"
            path.write_text(old + "\n" + "\n\n".join(results) + "\n", encoding="utf-8",
                            newline="\n")
            names.append(LOG_FILE)
        self.repo.git("add", "--", *names)
        self.repo.git("commit", "-m", message)
        remote = self.repo.push_verified()
        log.info("pushed %s (%s)", remote[:10], message)

    def cycle(self) -> datetime:
        now = datetime.now(self.tz)
        try:
            text = sync(self.repo, self.cfg, time.time())
        except (GitError, OSError) as exc:
            self.backoff = min(max(self.backoff * 2, self.cfg["poll_seconds"]),
                               self.cfg["max_backoff_seconds"])
            self.state.data.update(last_error=str(exc)[:400], last_error_at=iso_now())
            log.warning("sync failed; retrying in %ds: %s", self.backoff, exc)
            return now + timedelta(seconds=self.backoff)
        self.backoff = 0
        self.state.data.update(last_sync_at=iso_now(), last_commit=self.repo.head(),
                               last_error=None)
        blocks, quota, lines = parse(text)
        mode, wake, reason = choose_mode(quota, self.cfg, now, self.tz)
        self.state.data["quota"] = quota
        results: list[str] = []
        if mode == "normal" and not self.repo.dirty(PROMPT_FILE):
            lines, results = process(blocks, lines, self.state, now, self.tz, self.cfg)
        changed_mode = (mode, reason) != (self.state.data.get("mode"), self.state.data.get("reason"))
        if changed_mode:
            log.info("mode %s: %s; next wake %s", mode, reason, wake.isoformat(timespec="seconds"))
            lines = set_section(lines, "WORKER_STATUS", {
                "HOST": socket.gethostname(), "MODE": mode, "REASON": reason,
                "NEXT_WAKE": wake.isoformat(timespec="seconds"),
                "UPDATED_AT": now.isoformat(timespec="seconds")},
                "# Written by the background worker when its mode changes; agents read, never edit.")
        if (results or changed_mode) and not self.repo.dirty(PROMPT_FILE):
            try:
                self.write(lines, f"Worker: {mode}" + (f", {len(results)} steer(s)" if results else ""),
                           results)
                self.state.data["last_commit"] = self.repo.head()
            except GitError as exc:
                # The commit stays local; the next sync rebases and pushes it, or
                # stops on a conflict without overwriting anything.
                log.warning("could not publish the worker's update yet: %s", exc)
                return now + timedelta(seconds=self.cfg["poll_seconds"])
        self.state.data.update(mode=mode, reason=reason, next_wake=wake.isoformat())
        return wake

    def run(self, once: bool = False) -> None:
        self.state.data["started_at"] = iso_now()
        log.info("worker started on %s (pid %d)", socket.gethostname(), os.getpid())
        saved = self.state.data.get("next_wake")
        wake = datetime.now(self.tz)
        if saved and self.state.data.get("mode") in {"quota_wait", "fallback_wait"}:
            wake = max(wake, datetime.fromisoformat(saved))  # a passed wake fires at once
            log.info("restored %s; next wake %s", self.state.data["mode"], wake.isoformat())
        last_beat = time.time()
        while True:
            if datetime.now(self.tz) >= wake or self._wake_requested():
                try:
                    wake = self.cycle()
                except Exception:                     # never let one bad cycle stop the service
                    log.exception("cycle failed")
                    wake = datetime.now(self.tz) + timedelta(seconds=self.cfg["poll_seconds"])
                self.state.save()
                if once:
                    return
            if time.time() - last_beat >= self.cfg["heartbeat_seconds"]:
                log.info("alive; mode %s; next wake %s", self.state.data.get("mode"),
                         wake.isoformat(timespec="seconds"))
                last_beat = time.time()
            time.sleep(min(5.0, max(0.5, (wake - datetime.now(self.tz)).total_seconds())))

    def _wake_requested(self) -> bool:
        flag = self.state.root / "wake_now"
        if flag.exists():
            flag.unlink(missing_ok=True)
            log.info("immediate check requested")
            return True
        return False


# ------------------------------------------------------------------ process plumbing

def default_state_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "live_ai_agent"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "live_ai_agent"


def single_instance(root: Path):
    """Hold an OS lock for the life of the process; None if another worker has it."""
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "worker.lock").open("a+")
    try:
        if platform.system() == "Windows":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def setup_logging(root: Path, console: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(root / "worker.log", maxBytes=1_000_000,
                                                   backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    log.addHandler(handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)
    log.setLevel(logging.INFO)


def load_config(root: Path) -> dict:
    cfg = dict(DEFAULTS)
    path = root / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "once", "status", "wake"])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--state-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    root = args.state_dir or default_state_dir()
    if args.command == "status":
        path = root / "state.json"
        print(path.read_text(encoding="utf-8") if path.exists() else "no state yet")
        return 0
    if args.command == "wake":
        root.mkdir(parents=True, exist_ok=True)
        (root / "wake_now").write_text(iso_now(), encoding="utf-8")
        print("wake requested")
        return 0
    lock = single_instance(root)
    if lock is None:
        print("another worker is already running", file=sys.stderr)
        return 3
    setup_logging(root, console=sys.stdout is not None and sys.stdout.isatty())
    repo = args.repo.resolve()
    if not (repo / ".git").exists() or not (repo / PROMPT_FILE).exists():
        log.error("%s is not a checkout holding %s", repo, PROMPT_FILE)
        return 2
    try:
        Worker(repo, root, load_config(root)).run(once=args.command == "once")
    finally:
        log.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
