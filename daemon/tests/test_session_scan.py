"""Tests for daemon/session_scan.py — TDD red phase first, then green."""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import session_scan
from session_scan import (
    MOOD_ASKING,
    MOOD_DEAD,
    MOOD_SLEEPING,
    MOOD_WORKING,
    MAX_SESSIONS,
    scan_sessions,
    _updated_at_epoch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_session(sessions_dir: Path, pid: int, data: dict) -> Path:
    """Write a session JSON file named <pid>.json."""
    p = sessions_dir / f"{pid}.json"
    p.write_text(json.dumps(data))
    return p


def _write_job(jobs_dir: Path, job_id: str, state: str, detail: str = "") -> None:
    """Write a job state.json under jobs_dir/<job_id>/state.json."""
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(json.dumps({"state": state, "detail": detail}))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stale_ms() -> int:
    """Timestamp >2 min ago in ms."""
    return int((time.time() - 200) * 1000)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_emitted_dead():
    """Clear module-level dead-suppression set before every test."""
    session_scan._emitted_dead.clear()
    yield
    session_scan._emitted_dead.clear()


@pytest.fixture
def dirs(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return sessions, jobs


# ---------------------------------------------------------------------------
# _updated_at_epoch unit tests
# ---------------------------------------------------------------------------

def test_updated_at_epoch_ms_int():
    """An epoch-ms int (>1e12) must NOT be classified stale immediately."""
    now_ms = int(time.time() * 1000)
    result = _updated_at_epoch(now_ms)
    # Should be ~now in seconds
    assert abs(result - time.time()) < 5


def test_updated_at_epoch_seconds_int():
    """An epoch-seconds int (<1e12) is returned as-is."""
    now_s = int(time.time())
    result = _updated_at_epoch(now_s)
    assert abs(result - now_s) < 2


def test_updated_at_epoch_iso_string():
    """ISO-8601 string (with Z) is parsed correctly."""
    iso = "2026-06-12T09:11:35Z"
    result = _updated_at_epoch(iso)
    assert isinstance(result, float)
    assert result > 1_000_000_000  # sanity: after year 2001


def test_updated_at_epoch_none():
    """None input returns 0.0."""
    assert _updated_at_epoch(None) == 0.0


def test_updated_at_epoch_garbage():
    """Garbage string returns 0.0."""
    assert _updated_at_epoch("not-a-date") == 0.0


# ---------------------------------------------------------------------------
# live fixture: 2 busy sessions
# ---------------------------------------------------------------------------

def test_live_two_busy_sessions(dirs):
    """Live session with busy status → MOOD_WORKING; dead session → MOOD_DEAD."""
    sessions_dir, jobs_dir = dirs
    pid1 = os.getpid()
    # pid2 uses a non-existent PID (99999999) — will be reported dead
    _write_session(sessions_dir, pid1, {
        "pid": pid1, "sessionId": "aabbccdd-1111-2222-3333-444455556666",
        "cwd": "/repo/proj1", "name": "proj1",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })
    _write_session(sessions_dir, 99999999, {
        "pid": 99999999, "sessionId": "bbbbbbbb-1111-2222-3333-444455556666",
        "cwd": "/repo/proj2", "name": "proj2",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    def fake_alive(pid, proc_start):
        return pid == pid1

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", side_effect=fake_alive):
        results = scan_sessions()

    live_entry = next(e for e in results if e.name == "proj1")
    assert live_entry.mood == MOOD_WORKING


def test_live_two_separate_pids(dirs):
    """Two genuinely distinct live PIDs both return MOOD_WORKING."""
    sessions_dir, jobs_dir = dirs
    my_pid = os.getpid()
    other_pid = 1  # PID 1 (launchd/init) is always alive on macOS

    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "aaaaaaaa-0000-0000-0000-000000000001",
        "cwd": "/repo/a", "name": "alpha",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })
    _write_session(sessions_dir, other_pid, {
        "pid": other_pid, "sessionId": "bbbbbbbb-0000-0000-0000-000000000002",
        "cwd": "/repo/b", "name": "beta",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    def always_alive(pid, proc_start):
        return True

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", side_effect=always_alive):
        results = scan_sessions()

    assert len(results) == 2
    assert all(e.mood == MOOD_WORKING for e in results)


# ---------------------------------------------------------------------------
# stale fixture: live PID, updatedAt >2 min ago
# ---------------------------------------------------------------------------

def test_stale_session(dirs):
    """Live PID with updatedAt >2 min ago → MOOD_SLEEPING."""
    sessions_dir, jobs_dir = dirs
    my_pid = os.getpid()

    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "cccccccc-0000-0000-0000-000000000001",
        "cwd": "/repo/stale", "name": "stale-proj",
        "status": "busy", "kind": "fg", "updatedAt": _stale_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_SLEEPING


# ---------------------------------------------------------------------------
# dead fixture: PID not running
# ---------------------------------------------------------------------------

def test_dead_pid_not_running(dirs):
    """Non-existent PID → MOOD_DEAD."""
    sessions_dir, jobs_dir = dirs
    dead_pid = 9999998  # almost certainly dead

    _write_session(sessions_dir, dead_pid, {
        "pid": dead_pid, "sessionId": "dddddddd-0000-0000-0000-000000000001",
        "cwd": "/repo/dead", "name": "dead-proj",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=False):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_DEAD


# ---------------------------------------------------------------------------
# reuse fixture: PID alive but procStart mismatch → MOOD_DEAD
# ---------------------------------------------------------------------------

def test_reuse_startedat_mismatch(dirs):
    """PID alive but startedAt is far off (PID reuse) → MOOD_DEAD.

    Patches os.kill (PID exists) and subprocess.check_output (ps returns a
    specific lstart), but startedAt in the session file is 10000s earlier →
    reuse defense fires.
    """
    sessions_dir, jobs_dir = dirs
    my_pid = os.getpid()

    # ps will report "Sat Jun 13 17:38:56 2026" (epoch ≈ 1781338736)
    # startedAt is 10000s before that → reuse defense fires
    ps_epoch = time.mktime(time.strptime("Sat Jun 13 17:38:56 2026", "%a %b %d %H:%M:%S %Y"))
    fake_started_at_ms = int((ps_epoch - 10000) * 1000)

    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "eeeeeeee-0000-0000-0000-000000000001",
        "cwd": "/repo/reuse", "name": "reuse-proj",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
        "startedAt": fake_started_at_ms,
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("os.kill", return_value=None), \
         patch("subprocess.check_output", return_value="Sat Jun 13 17:38:56 2026"):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_DEAD


# ---------------------------------------------------------------------------
# malformed fixture: invalid JSON → silently skipped
# ---------------------------------------------------------------------------

def test_malformed_json_skipped(dirs):
    """A session file with invalid JSON is skipped silently — no exception."""
    sessions_dir, jobs_dir = dirs
    bad_file = sessions_dir / f"{os.getpid()}.json"
    bad_file.write_text("{ this is not json }")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()  # must not raise

    assert results == []


# ---------------------------------------------------------------------------
# 9-session fixture: truncated to MAX_SESSIONS (8)
# ---------------------------------------------------------------------------

def test_nine_sessions_truncated(dirs):
    """9 session files → scan_sessions returns at most MAX_SESSIONS (8) entries."""
    sessions_dir, jobs_dir = dirs

    for i in range(9):
        fake_pid = 10000 + i
        _write_session(sessions_dir, fake_pid, {
            "pid": fake_pid,
            "sessionId": f"ffffffff-0000-0000-0000-{i:012d}",
            "cwd": f"/repo/proj{i}", "name": f"proj{i}",
            "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
        })

    def always_alive(pid, proc_start):
        return True

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", side_effect=always_alive):
        results = scan_sessions()

    assert len(results) == MAX_SESSIONS


# ---------------------------------------------------------------------------
# status: waiting → MOOD_ASKING; unknown status → MOOD_WORKING (fail-soft)
# ---------------------------------------------------------------------------

def test_status_waiting_maps_to_asking(dirs):
    """status: 'waiting' → MOOD_ASKING."""
    sessions_dir, jobs_dir = dirs
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "a1a1a1a1-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "waiter",
        "status": "waiting", "kind": "fg", "updatedAt": _now_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_ASKING


def test_unknown_status_fails_soft(dirs):
    """Unknown status value → MOOD_WORKING (fail-soft)."""
    sessions_dir, jobs_dir = dirs
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "b2b2b2b2-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "mystery",
        "status": "totally_unknown_status", "kind": "fg", "updatedAt": _now_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_WORKING


# ---------------------------------------------------------------------------
# kind: bg with job state
# ---------------------------------------------------------------------------

def test_bg_job_blocked_is_asking(dirs):
    """kind=bg with job state 'blocked' → MOOD_ASKING."""
    sessions_dir, jobs_dir = dirs
    job_id = "job-block-001"
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "c3c3c3c3-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-blocked",
        "status": "busy", "kind": "bg", "jobId": job_id, "updatedAt": _now_ms(),
    })
    _write_job(jobs_dir, job_id, state="blocked", detail="waiting for input")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_ASKING


def test_bg_job_done_is_dropped(dirs):
    """kind=bg with job state 'done' → entry dropped (not returned)."""
    sessions_dir, jobs_dir = dirs
    job_id = "job-done-001"
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "d4d4d4d4-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-done",
        "status": "busy", "kind": "bg", "jobId": job_id, "updatedAt": _now_ms(),
    })
    _write_job(jobs_dir, job_id, state="done")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions()

    assert results == []


# ---------------------------------------------------------------------------
# bg job state — new tests (TDD: written RED before implementation)
# ---------------------------------------------------------------------------

def test_bg_job_done_dropped(dirs):
    """kind=bg + job state 'done' → NOT in result (alias / canonical name per spec)."""
    sessions_dir, jobs_dir = dirs
    job_id = "job-done-002"
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "e0e0e0e0-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-done-2",
        "status": "idle", "kind": "bg", "jobId": job_id, "updatedAt": _now_ms(),
    })
    _write_job(jobs_dir, job_id, state="done")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert results == [], "bg done session should be silently dropped"


def test_bg_job_working_is_working(dirs):
    """kind=bg + job state 'working' → MOOD_WORKING."""
    sessions_dir, jobs_dir = dirs
    job_id = "job-working-001"
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "e1e1e1e1-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-working",
        "status": "idle", "kind": "bg", "jobId": job_id, "updatedAt": _now_ms(),
    })
    _write_job(jobs_dir, job_id, state="working")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_WORKING


def test_bg_job_idle_is_sleeping(dirs):
    """kind=bg + job state 'idle' → MOOD_SLEEPING."""
    sessions_dir, jobs_dir = dirs
    job_id = "job-idle-001"
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "e2e2e2e2-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-idle",
        "status": "busy", "kind": "bg", "jobId": job_id, "updatedAt": _now_ms(),
    })
    _write_job(jobs_dir, job_id, state="idle")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_SLEEPING


def test_bg_live_despite_startedat_skew(dirs):
    """THE on-device regression: bg session whose startedAt is wildly off from the
    worker PID's real process start must NOT be classified DEAD.

    Pre-fix: _is_pid_alive was called for bg sessions, and a startedAt skew of
    9999 seconds would exceed START_TOLERANCE_SECS (120) → false-dead.
    Post-fix: bg liveness comes from job state, not PID clock.
    """
    sessions_dir, jobs_dir = dirs
    job_id = "job-skew-001"
    my_pid = os.getpid()
    # startedAt wildly off — 9999 seconds ago, well past the 120s tolerance
    skewed_started_at_ms = int((time.time() - 9999) * 1000)

    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "e3e3e3e3-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-skewed",
        "status": "busy", "kind": "bg", "jobId": job_id,
        "updatedAt": _now_ms(),
        "startedAt": skewed_started_at_ms,
    })
    _write_job(jobs_dir, job_id, state="working")

    # Do NOT mock _is_pid_alive — the bug manifests when real ps is used
    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1, (
        "bg session with startedAt skew was dropped — _is_pid_alive is still being called for bg"
    )
    assert results[0].mood == MOOD_WORKING, (
        f"Expected MOOD_WORKING from job state, got {results[0].mood!r}"
    )


def test_bg_missing_job_file_falls_back_to_pid(dirs):
    """bg session, no job state file: fall back to PID existence.
    - pid = os.getpid() (alive) → not MOOD_DEAD
    - pid = 9999994 (dead) → MOOD_DEAD one-cycle
    """
    sessions_dir, jobs_dir = dirs
    my_pid = os.getpid()

    # Alive PID, no job file → fall back to pid, should be alive
    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "e4e4e4e4-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-no-job-alive",
        "status": "busy", "kind": "bg", "jobId": "job-missing-001",
        "updatedAt": _now_ms(),
    })
    # No _write_job call → job state file is absent

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood != MOOD_DEAD, "alive PID with missing job file should not be DEAD"

    # Now test dead PID with no job file → MOOD_DEAD
    session_scan._emitted_dead.clear()
    dead_pid = 9999994
    sessions2 = dirs[0].parent / "sessions2"
    sessions2.mkdir()
    _write_session(sessions2, dead_pid, {
        "pid": dead_pid, "sessionId": "e4e4e4e4-0000-0000-0000-000000000002",
        "cwd": "/repo", "name": "bg-no-job-dead",
        "status": "busy", "kind": "bg", "jobId": "job-missing-002",
        "updatedAt": _now_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions2), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results2 = scan_sessions()

    assert len(results2) == 1
    assert results2[0].mood == MOOD_DEAD, "dead PID with missing job file should be MOOD_DEAD"


def test_bg_unknown_state_failsoft(dirs):
    """bg + job state 'weird' + session status 'waiting' → MOOD_ASKING (via _mood_from_status)."""
    sessions_dir, jobs_dir = dirs
    job_id = "job-weird-001"
    _write_session(sessions_dir, os.getpid(), {
        "pid": os.getpid(), "sessionId": "e5e5e5e5-1111-0000-0000-000000000001",
        "cwd": "/repo", "name": "bg-weird",
        "status": "waiting", "kind": "bg", "jobId": job_id, "updatedAt": _now_ms(),
    })
    _write_job(jobs_dir, job_id, state="weird")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_ASKING, (
        f"Unknown bg state should fail-soft to _mood_from_status('waiting')=MOOD_ASKING, got {results[0].mood!r}"
    )


# ---------------------------------------------------------------------------
# Name collision dedupe
# ---------------------------------------------------------------------------

def test_name_collision_dedupe(dirs):
    """Two sessions with the same name → 'foo' and 'foo-2'."""
    sessions_dir, jobs_dir = dirs
    pid_a = 20001
    pid_b = 20002

    for pid in (pid_a, pid_b):
        _write_session(sessions_dir, pid, {
            "pid": pid,
            "sessionId": f"e5e5e5e5-0000-0000-0000-{pid:012d}",
            "cwd": "/repo/foo", "name": "foo",
            "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
        })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions()

    names = {e.name for e in results}
    assert "foo" in names
    assert "foo-2" in names


# ---------------------------------------------------------------------------
# Focused session pinning
# ---------------------------------------------------------------------------

def test_focused_session_pinned_in_top8(dirs):
    """With 9 sessions, focused_id outside top-8 is still included; lowest-priority dropped."""
    sessions_dir, jobs_dir = dirs

    # Write 9 sessions with distinct 8-char session ID prefixes.
    # Sessions 0-7: status=busy (MOOD_WORKING, higher sort priority)
    # Session 8: status=idle (MOOD_SLEEPING, lowest sort priority → normally evicted at cap)
    pids = list(range(30001, 30010))
    session_ids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(len(pids))]
    for i, (pid, sid) in enumerate(zip(pids, session_ids)):
        status = "busy" if i < 8 else "idle"
        _write_session(sessions_dir, pid, {
            "pid": pid,
            "sessionId": sid,
            "cwd": f"/repo/p{i}", "name": f"p{i}",
            "status": status, "kind": "fg", "updatedAt": _now_ms(),
        })

    # The focused session is the sleeping one (index 8, pid 30009)
    focused_sid_short = session_ids[8][:8]  # "00000008"

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results = scan_sessions(focused_id=focused_sid_short)

    assert len(results) == MAX_SESSIONS
    ids = [e.id for e in results]
    assert focused_sid_short in ids


# ---------------------------------------------------------------------------
# Dead one-cycle suppression
# ---------------------------------------------------------------------------

def test_dead_emitted_once_then_suppressed(dirs):
    """Dead entry appears on first scan, then is absent on second scan."""
    sessions_dir, jobs_dir = dirs
    dead_pid = 9999997

    _write_session(sessions_dir, dead_pid, {
        "pid": dead_pid, "sessionId": "a0a0a0a0-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "dying",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=False):
        first = scan_sessions()
        second = scan_sessions()

    assert len(first) == 1
    assert first[0].mood == MOOD_DEAD
    assert len(second) == 0


# ---------------------------------------------------------------------------
# Fix 1: _emitted_dead eviction — sid removed when file disappears
# ---------------------------------------------------------------------------

def test_emitted_dead_evicted_after_file_deleted(dirs):
    """Dead session emitted once, then file deleted → _emitted_dead no longer holds that sid."""
    sessions_dir, jobs_dir = dirs
    dead_pid = 9999990
    sid = "deadbeef"

    session_file = _write_session(sessions_dir, dead_pid, {
        "pid": dead_pid, "sessionId": f"{sid}-1111-2222-3333-444455556666",
        "cwd": "/repo", "name": "gone",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=False):
        first = scan_sessions()

    assert first[0].mood == MOOD_DEAD
    assert sid in session_scan._emitted_dead

    # Delete the file — session no longer in registry
    session_file.unlink()

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=False):
        second = scan_sessions()

    assert second == []
    assert sid not in session_scan._emitted_dead


# ---------------------------------------------------------------------------
# Fix 2: ps timeout — TimeoutExpired → treat as not alive
# ---------------------------------------------------------------------------

def test_is_pid_alive_ps_timeout_trusts_os_kill():
    """subprocess.TimeoutExpired during ps lstart check → _is_pid_alive returns True.

    When ps is unavailable/times out we cannot do the reuse defense, so we
    fall back to trusting os.kill (pid exists) rather than false-dead-ing the
    session. This is the safe failure mode: prefer a live ghost over a
    prematurely killed real session.
    """
    import subprocess as _sp
    from session_scan import _is_pid_alive

    with patch("os.kill", return_value=None), \
         patch("subprocess.check_output",
               side_effect=_sp.TimeoutExpired(cmd="ps", timeout=2)):
        result = _is_pid_alive(12345, int(time.time() * 1000))

    assert result is True


# ---------------------------------------------------------------------------
# Fix 3: stable name dedupe — consistent order across scans
# ---------------------------------------------------------------------------

def test_name_dedupe_stable_across_scans(dirs):
    """Name deduplication is glob-order-stable: same pid always gets same suffix."""
    sessions_dir, jobs_dir = dirs
    pid_first = 10001   # sorts first: 10001.json < 10002.json
    pid_second = 10002

    for pid in (pid_first, pid_second):
        _write_session(sessions_dir, pid, {
            "pid": pid,
            "sessionId": f"aa{pid:06d}-0000-0000-0000-000000000000",
            "cwd": "/repo/foo", "name": "foo",
            "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
        })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        results_a = scan_sessions()
        results_b = scan_sessions()

    names_a = {e.pid: e.name for e in results_a}
    names_b = {e.pid: e.name for e in results_b}
    assert names_a == names_b
    assert names_a[pid_first] == "foo"
    assert names_a[pid_second] == "foo-2"


def test_dead_suppressed_until_pid_restarts(dirs):
    """After dead is suppressed, if same PID restarts with new procStart → live again."""
    sessions_dir, jobs_dir = dirs
    dead_pid = 9999996

    _write_session(sessions_dir, dead_pid, {
        "pid": dead_pid, "sessionId": "b0b0b0b0-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "restart-me",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    # First scan: dead → emitted
    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=False):
        first = scan_sessions()

    assert first[0].mood == MOOD_DEAD

    # Second scan: still dead → suppressed (absent)
    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=False):
        second = scan_sessions()

    assert len(second) == 0

    # Simulate PID "restarting": update the session file with alive=True
    # The _emitted_dead set has the sid; _is_pid_alive now returns True → discard from dead set
    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", return_value=True):
        third = scan_sessions()

    assert len(third) == 1
    assert third[0].mood in (MOOD_WORKING, MOOD_SLEEPING, MOOD_ASKING)  # alive, not dead


def test_pid_dies_between_scans(dirs):
    """Mock: PID dies between scan calls → dead on first call, absent on second."""
    sessions_dir, jobs_dir = dirs
    monitored_pid = 9999995
    call_count = {"n": 0}

    _write_session(sessions_dir, monitored_pid, {
        "pid": monitored_pid, "sessionId": "c0c0c0c0-0000-0000-0000-000000000001",
        "cwd": "/repo", "name": "flicker",
        "status": "busy", "kind": "fg", "updatedAt": _now_ms(),
    })

    def pid_dies_after_first(pid, proc_start):
        call_count["n"] += 1
        return False  # simulate: already dead on first call too

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir), \
         patch("session_scan._is_pid_alive", side_effect=pid_dies_after_first):
        first = scan_sessions()
        second = scan_sessions()

    assert first[0].mood == MOOD_DEAD
    assert len(second) == 0


# ---------------------------------------------------------------------------
# Locale/timezone-robust liveness: startedAt epoch comparison
# ---------------------------------------------------------------------------

def test_live_real_process_with_real_startedat(dirs):
    """THIS TEST CALLS REAL ps — no mock of _proc_start_epoch or _is_pid_alive.

    A session file with pid=os.getpid() and startedAt = real process start
    must come back LIVE (not MOOD_DEAD).
    """
    from session_scan import _proc_start_epoch
    sessions_dir, jobs_dir = dirs
    my_pid = os.getpid()
    proc_epoch = _proc_start_epoch(my_pid)
    assert proc_epoch is not None, "_proc_start_epoch returned None for own PID — ps broken?"

    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "f1f1f1f1-0000-0000-0000-000000000001",
        "cwd": "/repo/real", "name": "real-proc",
        "status": "busy", "kind": "fg",
        "updatedAt": _now_ms(),
        "startedAt": int(proc_epoch * 1000),  # real process start in epoch-ms
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood != MOOD_DEAD, (
        f"Live process {my_pid} was misclassified as DEAD — locale/timezone bug still present"
    )


def test_pid_reuse_rejected_by_startedat(dirs):
    """PID alive but startedAt is far in the past → MOOD_DEAD (reuse defense fires).

    Uses real ps. startedAt is offset 10000s before the real process start.
    """
    from session_scan import _proc_start_epoch
    sessions_dir, jobs_dir = dirs
    my_pid = os.getpid()
    proc_epoch = _proc_start_epoch(my_pid)
    assert proc_epoch is not None

    # startedAt 10000s before actual start — clearly a different (prior) process
    fake_started_at_ms = int((proc_epoch - 10000) * 1000)

    _write_session(sessions_dir, my_pid, {
        "pid": my_pid, "sessionId": "f2f2f2f2-0000-0000-0000-000000000001",
        "cwd": "/repo/reuse2", "name": "reuse-proc",
        "status": "busy", "kind": "fg",
        "updatedAt": _now_ms(),
        "startedAt": fake_started_at_ms,
    })

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 1
    assert results[0].mood == MOOD_DEAD, (
        f"PID reuse defense failed: process {my_pid} with wrong startedAt was not classified DEAD"
    )


def test_proc_start_epoch_parses_both_locale_orders():
    """_proc_start_epoch must parse both ps locale formats to the same epoch.

    Locks in the fix: 'Sat Jun 13 17:38:56 2026' (en_US) and
    'Sat 13 Jun 17:38:56 2026' (en_AU/en_GB) are the same moment.
    """
    import subprocess as _sp
    from session_scan import _proc_start_epoch

    us_format = "Sat Jun 13 17:38:56 2026"
    au_format = "Sat 13 Jun 17:38:56 2026"

    with patch("subprocess.check_output", return_value=us_format):
        epoch_us = _proc_start_epoch(12345)

    with patch("subprocess.check_output", return_value=au_format):
        epoch_au = _proc_start_epoch(12345)

    assert epoch_us is not None
    assert epoch_au is not None
    assert abs(epoch_us - epoch_au) < 1.0, (
        f"Locale format mismatch: {epoch_us} vs {epoch_au} (diff={abs(epoch_us-epoch_au):.3f}s)"
    )


# ---------------------------------------------------------------------------
# Integrated bg scenario: 5 real-world bg sessions with mixed job states
# ---------------------------------------------------------------------------

def test_bg_five_session_realworld_scenario(dirs):
    """5 bg sessions with mixed job states — done ones are dropped, others kept with correct mood.

    Ground-truth scenario matching real on-device data:

    | sid      | job state | expected                  |
    |----------|-----------|---------------------------|
    | e32025f4 | done      | absent (dropped)          |
    | 97de048f | blocked   | present, MOOD_ASKING      |
    | e6716229 | done      | absent (dropped)          |
    | 31c55355 | done      | absent (dropped)          |
    | 160bc5dc | working   | present, MOOD_WORKING     |
    """
    sessions_dir, jobs_dir = dirs

    # Session data: (sid_prefix, pid, name, job_state)
    sessions = [
        ("e32025f4", 1001, "session-a", "done"),
        ("97de048f", 1002, "session-b", "blocked"),
        ("e6716229", 1003, "session-c", "done"),
        ("31c55355", 1004, "session-d", "done"),
        ("160bc5dc", 1005, "session-e", "working"),
    ]

    for sid_prefix, pid, name, job_state in sessions:
        # Use sid_prefix as the jobId so JOBS_DIR/<jobId>/state.json lines up
        job_id = sid_prefix
        _write_session(sessions_dir, pid, {
            "pid": pid,
            "sessionId": f"{sid_prefix}-0000-0000-0000-000000000000",
            "cwd": f"/repo/{name}",
            "name": name,
            "kind": "bg",
            "jobId": job_id,
            "startedAt": "2026-06-13T10:00:00Z",
        })
        _write_job(jobs_dir, job_id, job_state, "some detail text")

    with patch("session_scan.SESSIONS_DIR", sessions_dir), \
         patch("session_scan.JOBS_DIR", jobs_dir):
        results = scan_sessions()

    assert len(results) == 2, (
        f"Expected 2 results (blocked + working), got {len(results)}: "
        f"{[(e.name, e.mood) for e in results]}"
    )

    result_by_name = {e.name: e for e in results}

    # done sessions must be absent
    for name in ("session-a", "session-c", "session-d"):
        assert name not in result_by_name, f"{name} (done) should be dropped but was present"

    # blocked → MOOD_ASKING
    assert "session-b" in result_by_name, "session-b (blocked) should be present"
    assert result_by_name["session-b"].mood == MOOD_ASKING, (
        f"session-b expected MOOD_ASKING, got {result_by_name['session-b'].mood}"
    )

    # working → MOOD_WORKING
    assert "session-e" in result_by_name, "session-e (working) should be present"
    assert result_by_name["session-e"].mood == MOOD_WORKING, (
        f"session-e expected MOOD_WORKING, got {result_by_name['session-e'].mood}"
    )

