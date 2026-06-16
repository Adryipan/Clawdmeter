"""Subscribe to FOCUS_CHAR; parse events onto asyncio.Queue; raise windows."""
import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

FOCUS_CHAR_UUID = "4c41555a-4465-7669-6365-000000000005"


@dataclass
class FocusEvent:
    kind: str          # "focus" | "clear" | "btn"
    id: Optional[str] = None


@dataclass
class ResolverCache:
    pid: int
    app: str           # "Terminal" | "iTerm2"
    tty: str


def _parse_notify(data: bytearray) -> Optional[FocusEvent]:
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        log.warning("FOCUS_CHAR: unparseable payload: %r", bytes(data)[:64])
        return None
    if "btn" in obj:
        return FocusEvent(kind="btn")
    if "focus" in obj:
        fid = obj["focus"]
        if fid is None:
            return FocusEvent(kind="clear")
        return FocusEvent(kind="focus", id=str(fid)[:8])
    return None


async def _raise_window(cache: ResolverCache) -> None:
    tty = cache.tty
    if cache.app == "Terminal":
        script = f'''
        tell application "Terminal"
            repeat with w in windows
                repeat with t in tabs of w
                    if (tty of t) contains "{tty}" then
                        set selected of t to true
                        set frontmost of w to true
                        activate
                        return
                    end if
                end repeat
            end repeat
        end tell
        '''
    elif cache.app == "iTerm2":
        script = f'''
        tell application "iTerm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if (tty) of s contains "{tty}" then
                            tell w to select tab t
                            activate
                            return
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        '''
    else:
        log.info("FOCUS: unsupported terminal app %r, skipping raise", cache.app)
        return
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, timeout=5
        )
    except Exception as e:
        log.warning("FOCUS: AppleScript raise failed: %s", e)


_KNOWN_TERMINALS = {"Terminal", "iTerm2"}


def _ppid(pid: int) -> Optional[int]:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()
        return int(out) if out else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None


def _process_name(pid: int) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["ps", "-o", "comm=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _resolve_tty(pid: int) -> Optional[ResolverCache]:
    """Walk parent-process chain from claude PID to find the enclosing terminal app.
    If the walk reaches launchd (pid 1) without matching a known terminal, returns None
    (unsupported terminal host — window-raise is skipped, HID floor still works).
    No pgrep/blind fallback: a blind guess cannot verify the tty and would raise the
    wrong window when e.g. iTerm2 and Terminal.app are both running under tmux."""
    try:
        tty = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    # Walk parent chain up to launchd looking for a known terminal app
    cur = pid
    seen: set[int] = set()
    while cur and cur not in seen and cur > 1:
        seen.add(cur)
        name = _process_name(cur)
        if name:
            # ps comm= may include full path; take basename
            base = name.split("/")[-1]
            for app in _KNOWN_TERMINALS:
                if base == app or base.startswith(app):
                    log.info("FOCUS: resolved %s via parent chain (pid %d)", app, cur)
                    return ResolverCache(pid=pid, app=app, tty=tty)
        cur = _ppid(cur) or 0

    log.info("FOCUS: unsupported terminal host for pid %d, skipping window-raise", pid)
    return None


async def _consumer(
    queue: asyncio.Queue,
    pid_map: dict,
    cache_map: dict,
    focused_ref: list,
) -> None:
    while True:
        event: FocusEvent = await queue.get()
        try:
            if event.kind == "focus":
                focused_ref[0] = event.id
                pid = pid_map.get(event.id)
                if pid:
                    # Approved deviation: wrap blocking ps calls in to_thread
                    cache = await asyncio.to_thread(_resolve_tty, pid)
                    if cache:
                        cache_map[event.id] = cache
                        await _raise_window(cache)
            elif event.kind == "clear":
                focused_ref[0] = None
            elif event.kind == "btn":
                fid = focused_ref[0]
                if fid and fid in cache_map:
                    await _raise_window(cache_map[fid])
        except Exception as e:
            log.error("FOCUS: consumer error handling %r: %s", event, e)
        finally:
            queue.task_done()


def make_notify_callback(queue: asyncio.Queue):
    def _cb(_char, data: bytearray) -> None:
        event = _parse_notify(data)
        if event:
            queue.put_nowait(event)
    return _cb


async def start_focus_listener(client, pid_map: dict, focused_ref: list) -> asyncio.Task:
    queue: asyncio.Queue = asyncio.Queue()
    cache_map: dict = {}
    cb = make_notify_callback(queue)
    await client.start_notify(FOCUS_CHAR_UUID, cb)
    task = asyncio.create_task(
        _consumer(queue, pid_map, cache_map, focused_ref)
    )
    return task


async def stop_focus_listener(client, task: asyncio.Task) -> None:
    try:
        await client.stop_notify(FOCUS_CHAR_UUID)
    except Exception:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
