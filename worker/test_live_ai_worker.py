"""Tests for the background worker, against a local bare repository standing in for GitHub.

    python -m pytest worker/test_live_ai_worker.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_ai_worker as w  # noqa: E402

TZ = w.tz_for("Asia/Kolkata")
NOW = datetime(2026, 10, 13, 20, 0, tzinfo=TZ)

QUEUE = """---
title: test
---
[AGENT_RULES]
rules
[END_AGENT_RULES]

# [ PROMPT ]
# PROMPT_ID: EXAMPLE-001
# STATUS: READY
# RUN_AT: 2020-01-01
# END_PROMPT

# =================== [ HOW TO ADD A TASK ] ===================

[ PROMPT ]
PROMPT_ID: T-1
RUN_AT: 2026-10-13T19:00:00+05:30
REPEAT: NONE
PRIORITY: NORMAL
STATUS: READY
TITLE: due task
PROMPT_TEXT:
STATUS: this line is prompt text, not a field
END_PROMPT

[ PROMPT ]
PROMPT_ID: T-2
RUN_AT: 2026-10-14
STATUS: READY
PROMPT_TEXT:
later
END_PROMPT

[ STEER ]
TARGET: T-2
ACTION: PAUSE
NOTE: hold it
END_STEER
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(seed)],
                   check=True)
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "t")
    git(seed, "config", "core.autocrlf", "false")     # the test controls line endings
    (seed / w.PROMPT_FILE).write_bytes(QUEUE.encode())         # LF, whatever the OS
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "HEAD:main")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(local)],
                   check=True)
    git(local, "config", "user.email", "w@example.com")
    git(local, "config", "user.name", "worker")
    git(local, "config", "core.autocrlf", "false")
    cfg = {**w.DEFAULTS, "local_edit_quiet_seconds": 0}
    worker = w.Worker(local, tmp_path / "state", cfg)
    return {"origin": origin, "seed": seed, "local": local, "worker": worker, "tmp": tmp_path}


def remote_text(world):
    git(world["seed"], "pull", "-q", "--ff-only", "origin", "main")
    return (world["seed"] / w.PROMPT_FILE).read_text(encoding="utf-8")


def test_only_live_blocks_parse_and_prompt_text_is_not_a_field():
    blocks, quota, _ = w.parse(QUEUE)
    assert [(b.kind, b.fields.get("PROMPT_ID", b.fields.get("TARGET"))) for b in blocks] == [
        ("PROMPT", "T-1"), ("PROMPT", "T-2"), ("STEER", "T-2")]
    assert blocks[0].fields["STATUS"] == "READY" and quota == {}


def test_a_cycle_notes_due_work_once_applies_a_steer_and_publishes(world, monkeypatch):
    monkeypatch.setattr(w, "datetime", _Frozen)
    worker = world["worker"]
    worker.cycle()
    text = remote_text(world)
    assert "STEER_STATUS: STEER_DONE" in text
    t2 = text[text.index("PROMPT_ID: T-2"):]
    assert "STATUS: PAUSED" in t2.split("END_PROMPT")[0]
    assert "PROMPT_ID: T-1" in text and text.count("STATUS: READY") == 2   # T-1 + the example
    assert "[WORKER_STATUS]" in text and "MODE: normal" in text
    log = (world["seed"] / w.LOG_FILE).read_text(encoding="utf-8")
    assert "STEER_RESULT" in log and "T-2: READY -> PAUSED" in log
    ledger = (world["tmp"] / "state" / "ledger.jsonl").read_text(encoding="utf-8")
    assert "T-1@2026-10-13T19:00:00+05:30" in ledger and "T-2@" not in ledger
    head = git(world["origin"], "rev-parse", "main")
    worker.cycle()                                  # nothing new: no commit, no duplicate
    assert git(world["origin"], "rev-parse", "main") == head
    assert ledger == (world["tmp"] / "state" / "ledger.jsonl").read_text(encoding="utf-8")


def test_a_rejected_push_is_rebased_and_verified(world, monkeypatch):
    monkeypatch.setattr(w, "datetime", _Frozen)
    worker = world["worker"]
    worker.cycle()
    # The other machine adds a task at the end while this one changes the file.
    remote_text(world)
    seed_file = world["seed"] / w.PROMPT_FILE
    seed_file.write_text(seed_file.read_text(encoding="utf-8") +
                         "\n[ STEER ]\nTARGET: T-1\nACTION: CANCEL\nNOTE: x\nEND_STEER\n",
                         encoding="utf-8")
    git(world["seed"], "commit", "-qam", "controller adds a steer")
    git(world["seed"], "push", "-q", "origin", "HEAD:main")
    worker.cycle()                                  # fetch, see the steer, apply, push
    text = remote_text(world)
    assert text.count("STEER_STATUS: STEER_DONE") == 2
    assert git(world["origin"], "rev-parse", "main") == git(world["local"], "rev-parse", "HEAD")


def test_a_conflict_overwrites_nothing(world, monkeypatch):
    monkeypatch.setattr(w, "datetime", _Frozen)
    worker = world["worker"]
    local_file = world["local"] / w.PROMPT_FILE
    local_file.write_text(QUEUE.replace("TITLE: due task", "TITLE: local wording"), encoding="utf-8")
    seed_file = world["seed"] / w.PROMPT_FILE
    seed_file.write_text(QUEUE.replace("TITLE: due task", "TITLE: remote wording"), encoding="utf-8")
    git(world["seed"], "commit", "-qam", "remote edit")
    git(world["seed"], "push", "-q", "origin", "HEAD:main")
    before = git(world["origin"], "rev-parse", "main")
    worker.cycle()
    assert git(world["origin"], "rev-parse", "main") == before
    assert "conflict" in worker.state.data["last_error"]
    assert "local wording" in local_file.read_text(encoding="utf-8")
    assert not (world["local"] / ".git" / "rebase-merge").exists()


def test_a_network_failure_backs_off_and_recovers(world, monkeypatch):
    monkeypatch.setattr(w, "datetime", _Frozen)
    worker = world["worker"]
    git(world["local"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    first = worker.cycle()
    second = worker.cycle()
    assert worker.state.data["last_error"]
    assert (second - NOW) > (first - NOW) >= timedelta(seconds=60)
    git(world["local"], "remote", "set-url", "origin", str(world["origin"]))
    worker.cycle()
    assert worker.state.data["last_error"] is None and worker.backoff == 0


@pytest.mark.parametrize("quota,mode,wake", [
    ({}, "normal", NOW + timedelta(seconds=60)),
    ({"STATUS": "EXHAUSTED", "RESET_AT": "2026-10-13T23:30:00+05:30", "RESET_VERIFIED": "YES"},
     "quota_wait", datetime(2026, 10, 13, 23, 31, tzinfo=TZ)),
    ({"STATUS": "EXHAUSTED", "RESET_AT": "2026-10-13T23:30:00+05:30", "RESET_VERIFIED": "NO"},
     "fallback_wait", NOW + timedelta(seconds=1800)),
    ({"STATUS": "NEAR_LIMIT", "RESET_AT": "UNKNOWN"}, "fallback_wait", NOW + timedelta(seconds=1800)),
    ({"STATUS": "EXHAUSTED", "RESET_AT": "2026-10-13T19:30:00+05:30", "RESET_VERIFIED": "YES"},
     "normal", NOW + timedelta(seconds=60)),
])
def test_quota_modes(quota, mode, wake):
    got_mode, got_wake, reason = w.choose_mode(quota, w.DEFAULTS, NOW, TZ)
    assert (got_mode, got_wake) == (mode, wake), reason


def test_quota_wait_stops_polling_until_the_reset_then_resumes(world, monkeypatch):
    """Simulated clock: no fetch while waiting; after the reset, normal polling returns."""
    seed_file = world["seed"] / w.PROMPT_FILE
    seed_file.write_text(QUEUE + "\n[QUOTA_STATUS]\nPROVIDER: test\nSTATUS: EXHAUSTED\n"
                         "RESET_AT: 2026-10-13T20:10:00+05:30\nRESET_VERIFIED: YES\n"
                         "[END_QUOTA_STATUS]\n", encoding="utf-8")
    git(world["seed"], "commit", "-qam", "agent records quota")
    git(world["seed"], "push", "-q", "origin", "HEAD:main")
    clock = {"now": NOW}
    monkeypatch.setattr(w, "datetime", _clocked(clock))
    worker = world["worker"]
    wake = worker.cycle()
    assert worker.state.data.get("mode") == "quota_wait", worker.state.data
    assert wake == datetime(2026, 10, 13, 20, 11, tzinfo=TZ)
    assert "MODE: quota_wait" in remote_text(world)
    fetches = []
    real = worker.repo.git
    monkeypatch.setattr(worker.repo, "git", lambda *a, **k: fetches.append(a) or real(*a, **k))
    clock["now"] = NOW + timedelta(minutes=5)      # still waiting: the loop would not call cycle
    assert clock["now"] < wake and not fetches
    clock["now"] = wake                             # the scheduled wake
    worker.cycle()
    assert worker.state.data["mode"] == "normal" and "passed" in worker.state.data["reason"]
    assert any(a[0] == "fetch" for a in fetches)


def test_restart_restores_a_wait_and_a_passed_wake_fires_at_once(tmp_path):
    state = w.State(tmp_path)
    state.data.update(mode="quota_wait", next_wake="2026-10-13T19:00:00+05:30")
    state.save()
    again = w.State(tmp_path)
    assert again.data["mode"] == "quota_wait"
    assert again.record("k", "x") and not w.State(tmp_path).record("k", "x")


def test_one_worker_per_machine(tmp_path):
    first = w.single_instance(tmp_path)
    assert first is not None
    probe = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, r'%s'); import live_ai_worker as w; "
                            "print(w.single_instance(__import__('pathlib').Path(r'%s')) is None)"
                            % (Path(w.__file__).parent, tmp_path)],
                           capture_output=True, text=True)
    assert probe.stdout.strip() == "True"
    first.close()


def test_crlf_files_stay_crlf(world, monkeypatch):
    monkeypatch.setattr(w, "datetime", _Frozen)
    seed_file = world["seed"] / w.PROMPT_FILE
    seed_file.write_bytes(QUEUE.replace("\n", "\r\n").encode())
    git(world["seed"], "commit", "-qam", "crlf")
    git(world["seed"], "push", "-q", "origin", "HEAD:main")
    world["worker"].cycle()
    raw = (world["local"] / w.PROMPT_FILE).read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def _clocked(clock):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)
    return Clock
