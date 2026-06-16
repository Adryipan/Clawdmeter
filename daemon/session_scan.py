"""Scan Claude Code session registry and return mood-annotated session list."""
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
JOBS_DIR = Path.home() / ".claude" / "jobs"
STALE_SECS = 120
MAX_SESSIONS = 8

MOOD_WORKING = "w"
MOOD_ASKING  = "a"
MOOD_SLEEPING = "s"
MOOD_DEAD    = "d"

STATUS_TO_MOOD = {
    "busy":    MOOD_WORKING,
    "waiting": MOOD_ASKING,
    "idle":    MOOD_SLEEPING,
}
MOOD_PRIORITY = {MOOD_ASKING: 0, MOOD_DEAD: 1, MOOD_WORKING: 2, MOOD_SLEEPING: 3}

# Module-level set: session ids emitted as DEAD this scan cycle.
# Suppresses re-emission on subsequent scans until PID goes live again.
_emitted_dead: set[str] = set()


@dataclass
class SessionEntry:
    id: str           # first 8 chars of sessionId
    name: str         # display name, deduplicated
    mood: str         # w/a/s/d
    detail: str = ""  # tool/sub-text, ≤16 chars
    updated_at: float = 0.0
    pid: int = 0


def _updated_at_epoch(value) -> float:
    """Convert updatedAt field to epoch seconds (float).

    Handles:
    - int/float > 1e12  → epoch milliseconds (divide by 1000)
    - int/float <= 1e12 → epoch seconds (return as-is)
    - ISO-8601 string   → parse (Z → +00:00)
    - None/garbage      → 0.0
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        if value > 1e12:
            return value / 1000.0
        return float(value)
    if isinstance(value, str):
        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            return dt.timestamp()
        except (ValueError, AttributeError):
            return 0.0
    return 0.0


# START_TOLERANCE_SECS: the legitimate skew between startedAt (epoch-ms,
# recorded by the Claude Code process at registration time) and the OS
# process start reported by ps (truncated to whole seconds, local clock).
# Empirically measured on the deployment machine: genuine live sessions
# show <1s skew; recycled PIDs show 7+ minutes to 7+ hours of skew.
# 120s absorbs all realistic registration lag while still rejecting reuse.
START_TOLERANCE_SECS = 120


def _proc_start_epoch(pid: int) -> Optional[float]:
    """Return the process's start time as a local epoch (seconds), or None if unobtainable.

    ps -o lstart= prints LOCAL time; its field order is locale-dependent:
      en_US: 'Sat Jun 13 17:38:56 2026'  (month before day)
      en_AU: 'Sat 13 Jun 17:38:56 2026'  (day before month)
    We try both strptime formats so the result is locale-independent.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(out, fmt))  # local time → epoch
        except ValueError:
            continue
    return None


def _is_pid_alive(pid: int, started_at_ms: Optional[float]) -> bool:
    """Return True if pid exists AND its start time matches started_at_ms (defeats PID reuse).

    Uses epoch-ms numeric comparison (timezone/locale-independent) instead of
    string-matching the procStart field, which varied by locale and UTC-vs-local
    offset and caused all live sessions to be misclassified as DEAD.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    if not started_at_ms:
        return True  # no recorded start → trust os.kill; can't do reuse defense
    actual = _proc_start_epoch(pid)
    if actual is None:
        return True  # ps unparseable → do NOT false-dead; trust os.kill
    return abs(actual - started_at_ms / 1000.0) <= START_TOLERANCE_SECS


def _mood_from_status(status: Optional[str]) -> str:
    if status is None:
        return MOOD_WORKING
    return STATUS_TO_MOOD.get(status, MOOD_WORKING)


def _pid_exists(pid: int) -> bool:
    """Return True if the pid exists (no start-time tolerance — for bg fallback only)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def scan_sessions(focused_id: Optional[str] = None) -> list[SessionEntry]:
    """Return mood-annotated sessions, capped at MAX_SESSIONS.

    Foreground sessions (kind != 'bg'):
        Liveness via _is_pid_alive(pid, startedAt) — uses start-time tolerance
        to defeat PID reuse. Dead → MOOD_DEAD once-cycle. Stale → MOOD_SLEEPING.
        Otherwise mood from session status field.

    Background sessions (kind == 'bg'):
        Liveness and mood come from ~/.claude/jobs/<jobId>/state.json, NOT the
        PID start time. The session-file PID is a worker whose start time differs
        from startedAt by minutes to hours, so the fg PID-clock check must not
        be applied. If the job state file is missing, fall back to raw PID
        existence (os.kill) without start-time tolerance.

    Dead sessions are emitted once, then suppressed until the session goes live again.
    """
    entries: list[SessionEntry] = []
    names_seen: dict[str, int] = {}
    seen_sids: set[str] = set()

    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("sessionId", "")
        sid = session_id[:8] if session_id else path.stem[:8]
        seen_sids.add(sid)
        raw_name = data.get("name") or Path(data.get("cwd", path.stem)).name
        updated_at = _updated_at_epoch(data.get("updatedAt"))
        detail = ""

        if data.get("kind") == "bg":
            # --- Background session: mood from job state file, not PID clock ---
            job_id = data.get("jobId", "")
            job_state_path = JOBS_DIR / job_id / "state.json"
            try:
                jdata = json.loads(job_state_path.read_text())
                jstate = jdata.get("state", "")
                if jstate == "done":
                    continue  # drop completed bg sessions silently
                elif jstate == "blocked":
                    mood = MOOD_ASKING
                elif jstate in ("working", "active"):
                    mood = MOOD_WORKING
                elif jstate == "idle":
                    mood = MOOD_SLEEPING
                else:
                    # Unknown non-empty state — fail-soft to session status
                    mood = _mood_from_status(data.get("status"))
                detail = (jdata.get("detail") or "")[:16]
            except (OSError, json.JSONDecodeError):
                # Job state file missing or unreadable — fall back to raw PID existence
                # (no start-time tolerance; startedAt skew is expected for bg workers)
                if not _pid_exists(pid):
                    if sid in _emitted_dead:
                        continue
                    mood = MOOD_DEAD
                    detail = ""
                else:
                    mood = _mood_from_status(data.get("status"))

            if mood != MOOD_DEAD:
                seen_sids.add(sid)
                _emitted_dead.discard(sid)
            if mood == MOOD_DEAD:
                _emitted_dead.add(sid)

        else:
            # --- Foreground session: liveness via startedAt PID-reuse defense ---
            started_at_ms = data.get("startedAt")
            if not _is_pid_alive(pid, started_at_ms):
                if sid in _emitted_dead:
                    continue  # already shown dead once — drop silently
                mood = MOOD_DEAD
                detail = ""
                _emitted_dead.add(sid)
            else:
                _emitted_dead.discard(sid)
                age = time.time() - updated_at
                if age > STALE_SECS:
                    mood = MOOD_SLEEPING
                else:
                    mood = _mood_from_status(data.get("status"))
                    detail = data.get("detail", "")[:16]

        # Deduplicate names
        if raw_name in names_seen:
            names_seen[raw_name] += 1
            display_name = f"{raw_name}-{names_seen[raw_name]}"
        else:
            names_seen[raw_name] = 1
            display_name = raw_name

        entries.append(SessionEntry(
            id=sid,
            name=display_name[:16],
            mood=mood,
            detail=detail,
            updated_at=updated_at,
            pid=pid,
        ))

    # Evict sids no longer present in the registry (prevents unbounded growth)
    _emitted_dead.intersection_update(seen_sids)

    # Sort: mood priority asc, then updatedAt desc
    entries.sort(key=lambda e: (MOOD_PRIORITY.get(e.mood, 99), -e.updated_at))

    # Cap to MAX_SESSIONS, pinning focused entry
    if len(entries) > MAX_SESSIONS:
        if focused_id:
            focused = [e for e in entries if e.id == focused_id]
            rest = [e for e in entries if e.id != focused_id]
            entries = focused + rest[:MAX_SESSIONS - len(focused)]
        else:
            entries = entries[:MAX_SESSIONS]

    return entries
