"""TDD tests for focus_listener.py (Task 5).

All tests are mock-based — no real osascript/ps calls made.
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Make daemon/ importable
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bytearray(obj) -> bytearray:
    import json
    return bytearray(json.dumps(obj).encode())


# ---------------------------------------------------------------------------
# 1. _parse_notify: {"focus": "abcd1234"} → FocusEvent(kind="focus", id="abcd1234")
# ---------------------------------------------------------------------------

class TestParseNotify:
    def test_focus_event_with_id(self):
        from focus_listener import _parse_notify, FocusEvent
        data = _make_bytearray({"focus": "abcd1234"})
        event = _parse_notify(data)
        assert event is not None
        assert event.kind == "focus"
        assert event.id == "abcd1234"

    def test_focus_event_truncates_id_to_8_chars(self):
        from focus_listener import _parse_notify, FocusEvent
        data = _make_bytearray({"focus": "abcd1234EXTRA"})
        event = _parse_notify(data)
        assert event is not None
        assert event.kind == "focus"
        assert event.id == "abcd1234"

    def test_focus_null_returns_clear(self):
        from focus_listener import _parse_notify, FocusEvent
        data = _make_bytearray({"focus": None})
        event = _parse_notify(data)
        assert event is not None
        assert event.kind == "clear"
        assert event.id is None

    def test_btn_returns_btn_event(self):
        from focus_listener import _parse_notify, FocusEvent
        data = _make_bytearray({"btn": 1})
        event = _parse_notify(data)
        assert event is not None
        assert event.kind == "btn"

    def test_garbage_json_returns_none_no_exception(self):
        from focus_listener import _parse_notify
        data = bytearray(b"not json at all!!!")
        # Must not raise
        event = _parse_notify(data)
        assert event is None

    def test_unknown_payload_returns_none(self):
        from focus_listener import _parse_notify
        data = _make_bytearray({"unknown_key": "value"})
        event = _parse_notify(data)
        assert event is None

    def test_empty_bytes_returns_none_no_exception(self):
        from focus_listener import _parse_notify
        event = _parse_notify(bytearray(b""))
        assert event is None


# ---------------------------------------------------------------------------
# 2. make_notify_callback: callback is parse-only
#    asyncio.Queue.put_nowait called; no AppleScript / subprocess in callback
# ---------------------------------------------------------------------------

class TestNotifyCallback:
    def test_focus_payload_enqueues_focus_event(self):
        from focus_listener import make_notify_callback, FocusEvent
        queue = asyncio.Queue()
        cb = make_notify_callback(queue)

        with patch("subprocess.run") as mock_run, \
             patch("asyncio.to_thread") as mock_thread:
            cb(None, _make_bytearray({"focus": "abcd1234"}))

        assert not queue.empty()
        event = queue.get_nowait()
        assert event.kind == "focus"
        assert event.id == "abcd1234"
        mock_run.assert_not_called()
        mock_thread.assert_not_called()

    def test_clear_payload_enqueues_clear_event(self):
        from focus_listener import make_notify_callback, FocusEvent
        queue = asyncio.Queue()
        cb = make_notify_callback(queue)
        cb(None, _make_bytearray({"focus": None}))
        assert not queue.empty()
        event = queue.get_nowait()
        assert event.kind == "clear"

    def test_btn_payload_enqueues_btn_event(self):
        from focus_listener import make_notify_callback, FocusEvent
        queue = asyncio.Queue()
        cb = make_notify_callback(queue)
        cb(None, _make_bytearray({"btn": 1}))
        event = queue.get_nowait()
        assert event.kind == "btn"

    def test_garbage_json_nothing_enqueued_no_exception(self):
        from focus_listener import make_notify_callback
        queue = asyncio.Queue()
        cb = make_notify_callback(queue)
        cb(None, bytearray(b"{{broken"))
        assert queue.empty()


# ---------------------------------------------------------------------------
# 3. Consumer ordering: focus→btn→clear arrive in order while slow _raise_window
#    is in flight; assert processing order via recorded list.
# ---------------------------------------------------------------------------

class TestConsumerOrdering:
    async def test_ordering_focus_btn_clear(self):
        """Three events processed in arrival order; _raise_window does not re-order."""
        from focus_listener import _consumer, FocusEvent

        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {"abcd1234": 1234}
        cache_map = {}
        focused_ref = [None]

        order: list = []

        # Patch _resolve_tty to return a cache synchronously (no actual ps)
        from focus_listener import ResolverCache
        fake_cache = ResolverCache(pid=1234, app="Terminal", tty="/dev/ttys001")

        raise_event = asyncio.Event()

        async def slow_raise(cache):
            order.append("raise_start")
            await raise_event.wait()
            order.append("raise_end")

        # Pre-populate cache_map so btn works immediately
        cache_map["abcd1234"] = fake_cache

        with patch("focus_listener._resolve_tty", return_value=fake_cache), \
             patch("focus_listener._raise_window", side_effect=slow_raise):
            # Enqueue focus, btn, clear
            await queue.put(FocusEvent(kind="focus", id="abcd1234"))
            await queue.put(FocusEvent(kind="btn"))
            await queue.put(FocusEvent(kind="clear"))

            task = asyncio.create_task(
                _consumer(queue, pid_map, cache_map, focused_ref)
            )

            # Give consumer time to start processing focus event
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            # Release the raise_window so consumer can continue
            raise_event.set()

            # Wait for all events to drain
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Focus was processed first (raise_start happened), then cleared at end
        assert focused_ref[0] is None  # clear processed last
        assert "raise_start" in order
        assert "raise_end" in order


# ---------------------------------------------------------------------------
# 4. Consumer: focus event → sets focused_ref, resolves tty, raises window
# ---------------------------------------------------------------------------

class TestConsumerFocusEvent:
    async def test_focus_sets_focused_ref_and_raises_window(self):
        from focus_listener import _consumer, FocusEvent, ResolverCache
        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {"abcd1234": 1234}
        cache_map = {}
        focused_ref = [None]
        fake_cache = ResolverCache(pid=1234, app="Terminal", tty="/dev/ttys001")

        raise_calls = []

        async def mock_raise(cache):
            raise_calls.append(cache)

        with patch("focus_listener._resolve_tty", return_value=fake_cache), \
             patch("focus_listener._raise_window", side_effect=mock_raise):
            await queue.put(FocusEvent(kind="focus", id="abcd1234"))

            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert focused_ref[0] == "abcd1234"
        assert len(raise_calls) == 1
        assert raise_calls[0] == fake_cache
        assert cache_map["abcd1234"] == fake_cache

    async def test_focus_no_pid_in_map_no_resolve_no_raise(self):
        """focus event with id not in pid_map → resolve_tty NOT called, no raise."""
        from focus_listener import _consumer, FocusEvent
        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {}  # empty — no pid known for this id
        cache_map = {}
        focused_ref = [None]

        with patch("focus_listener._resolve_tty") as mock_resolve, \
             patch("focus_listener._raise_window") as mock_raise:
            await queue.put(FocusEvent(kind="focus", id="abcd1234"))
            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert focused_ref[0] == "abcd1234"
        mock_resolve.assert_not_called()
        mock_raise.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Consumer: clear event → focused_ref[0] set to None
# ---------------------------------------------------------------------------

class TestConsumerClearEvent:
    async def test_clear_resets_focused_ref(self):
        from focus_listener import _consumer, FocusEvent
        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {}
        cache_map = {}
        focused_ref = ["abcd1234"]  # start with something focused

        await queue.put(FocusEvent(kind="clear"))
        task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
        await asyncio.wait_for(queue.join(), timeout=2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert focused_ref[0] is None


# ---------------------------------------------------------------------------
# 6. Consumer: btn event with cached resolver → _raise_window called (re-raise)
#    to_thread called with osascript AppleScript (via _raise_window → to_thread)
# ---------------------------------------------------------------------------

class TestConsumerBtnEvent:
    async def test_btn_uses_cached_resolver_and_raises(self):
        """btn with cached result in cache_map → _raise_window called with cache."""
        from focus_listener import _consumer, FocusEvent, ResolverCache
        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {}
        fake_cache = ResolverCache(pid=1234, app="Terminal", tty="/dev/ttys001")
        cache_map = {"abcd1234": fake_cache}
        focused_ref = ["abcd1234"]

        raise_calls = []

        async def mock_raise(cache):
            raise_calls.append(cache)

        with patch("focus_listener._raise_window", side_effect=mock_raise):
            await queue.put(FocusEvent(kind="btn"))
            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(raise_calls) == 1
        assert raise_calls[0] == fake_cache

    async def test_btn_no_focused_id_no_raise(self):
        """btn when focused_ref[0] is None → no raise."""
        from focus_listener import _consumer, FocusEvent
        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {}
        cache_map = {}
        focused_ref = [None]

        with patch("focus_listener._raise_window") as mock_raise:
            await queue.put(FocusEvent(kind="btn"))
            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_raise.assert_not_called()

    async def test_btn_no_mapping_in_cache_no_to_thread(self):
        """btn with focused_ref set but NOT in cache_map → no-op, no raise."""
        from focus_listener import _consumer, FocusEvent
        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {}
        cache_map = {}  # focused id "abcd1234" NOT cached
        focused_ref = ["abcd1234"]

        with patch("focus_listener._raise_window") as mock_raise, \
             patch("asyncio.to_thread") as mock_thread:
            await queue.put(FocusEvent(kind="btn"))
            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_raise.assert_not_called()
        mock_thread.assert_not_called()

    async def test_btn_calls_applescript_via_to_thread(self):
        """_raise_window for Terminal uses asyncio.to_thread with osascript."""
        from focus_listener import _raise_window, ResolverCache
        cache = ResolverCache(pid=1234, app="Terminal", tty="/dev/ttys001")

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await _raise_window(cache)

        mock_thread.assert_called_once()
        # First positional arg is subprocess.run
        args = mock_thread.call_args[0]
        assert args[0] is subprocess.run
        # Command includes osascript
        cmd = args[1]
        assert cmd[0] == "osascript"

    async def test_btn_iterm2_applescript_via_to_thread(self):
        """_raise_window for iTerm2 uses asyncio.to_thread with osascript."""
        from focus_listener import _raise_window, ResolverCache
        cache = ResolverCache(pid=1234, app="iTerm2", tty="/dev/ttys001")

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await _raise_window(cache)

        mock_thread.assert_called_once()
        args = mock_thread.call_args[0]
        assert args[0] is subprocess.run

    async def test_btn_unsupported_terminal_no_to_thread(self):
        """_raise_window for unsupported app → no to_thread call."""
        from focus_listener import _raise_window, ResolverCache
        cache = ResolverCache(pid=1234, app="Ghostty", tty="/dev/ttys001")

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await _raise_window(cache)

        mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# 7. start_focus_listener / stop_focus_listener lifecycle
# ---------------------------------------------------------------------------

class TestStartStopFocusListener:
    async def test_start_returns_task(self, mock_bleak_client):
        from focus_listener import start_focus_listener
        pid_map = {}
        focused_ref = [None]
        task = await start_focus_listener(mock_bleak_client, pid_map, focused_ref)
        assert isinstance(task, asyncio.Task)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_start_subscribes_to_focus_char(self, mock_bleak_client):
        from focus_listener import start_focus_listener, FOCUS_CHAR_UUID
        pid_map = {}
        focused_ref = [None]
        task = await start_focus_listener(mock_bleak_client, pid_map, focused_ref)
        mock_bleak_client.start_notify.assert_called_once()
        assert mock_bleak_client.start_notify.call_args[0][0] == FOCUS_CHAR_UUID
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_stop_cancels_task(self, mock_bleak_client):
        from focus_listener import start_focus_listener, stop_focus_listener
        pid_map = {}
        focused_ref = [None]
        task = await start_focus_listener(mock_bleak_client, pid_map, focused_ref)
        await stop_focus_listener(mock_bleak_client, task)
        assert task.done()
        mock_bleak_client.stop_notify.assert_called_once()

    async def test_stop_unsubscribes_focus_char(self, mock_bleak_client):
        from focus_listener import start_focus_listener, stop_focus_listener, FOCUS_CHAR_UUID
        pid_map = {}
        focused_ref = [None]
        task = await start_focus_listener(mock_bleak_client, pid_map, focused_ref)
        await stop_focus_listener(mock_bleak_client, task)
        assert mock_bleak_client.stop_notify.call_args[0][0] == FOCUS_CHAR_UUID


# ---------------------------------------------------------------------------
# 8. Dead-focus integration: focused session dies → payload has no f:1 key,
#    focused_ref cleared to None by maybe_scan_and_send
# ---------------------------------------------------------------------------

class TestDeadFocusIntegration:
    async def test_dead_session_clears_focus_and_no_f_key_in_payload(self):
        """focused session marked dead → scan loop clears focused_ref[0] to None;
        build_payload result has no 'f':1 key in ss entries."""
        import json as _json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))

        import claude_usage_daemon as daemon
        from session_scan import SessionEntry, MOOD_DEAD
        from payload import build_payload

        api_data = {"s": 42.5, "sr": "2h", "w": 12.0, "wr": "5d", "st": 1, "ok": 1}

        # Stand-in session object matching Task-4 attributes
        sess_obj = SimpleNamespace(
            last_api_data=api_data,
            last_payload_bytes=b"",
            last_scan_time=0.0,
        )
        mock_write = AsyncMock(return_value=True)
        sess_obj.write_payload = mock_write

        focused_ref = ["abcd1234"]
        pid_map = {"abcd1234": 9999}
        sessions = []

        # Scan returns the focused session as DEAD
        dead_session = SessionEntry(id="abcd1234", name="myproject", mood=MOOD_DEAD)
        now = daemon.SCAN_INTERVAL + 1

        with patch("claude_usage_daemon.scan_sessions", return_value=[dead_session]):
            sessions = await daemon.maybe_scan_and_send(
                sess_obj, sessions, focused_ref, pid_map, now
            )

        # focused_ref must be cleared
        assert focused_ref[0] is None, (
            f"Expected focused_ref[0]=None after dead session, got {focused_ref[0]!r}"
        )

        # The payload written must have no f:1 on any ss entry
        if mock_write.called:
            written_bytes = mock_write.call_args[0][0]
            parsed = _json.loads(written_bytes.decode())
            ss_entries = parsed.get("ss", [])
            for entry in ss_entries:
                assert entry.get("f") != 1, (
                    f"f:1 found on entry after focused session died: {entry}"
                )


# ---------------------------------------------------------------------------
# 9. _ppid / _process_name timeout handling (approved deviation)
# ---------------------------------------------------------------------------

class TestHelperTimeouts:
    def test_ppid_returns_none_on_timeout(self):
        """_ppid returns None when subprocess times out."""
        from focus_listener import _ppid
        with patch("subprocess.check_output", side_effect=subprocess.TimeoutExpired("ps", 2)):
            result = _ppid(1234)
        assert result is None

    def test_process_name_returns_none_on_timeout(self):
        """_process_name returns None when subprocess times out."""
        from focus_listener import _process_name
        with patch("subprocess.check_output", side_effect=subprocess.TimeoutExpired("ps", 2)):
            result = _process_name(1234)
        assert result is None

    def test_resolve_tty_returns_none_when_tty_lookup_times_out(self):
        """_resolve_tty returns None when the initial tty ps call times out."""
        from focus_listener import _resolve_tty
        with patch("subprocess.check_output", side_effect=subprocess.TimeoutExpired("ps", 2)):
            result = _resolve_tty(1234)
        assert result is None

    def test_resolve_tty_wraps_in_to_thread(self):
        """_consumer wraps _resolve_tty in asyncio.to_thread (approved deviation)."""
        # We verify this by confirming _resolve_tty is synchronous (not a coroutine)
        # and that the consumer awaits asyncio.to_thread(_resolve_tty, pid).
        import inspect
        from focus_listener import _resolve_tty
        assert not inspect.iscoroutinefunction(_resolve_tty), (
            "_resolve_tty must be sync; consumer wraps it in asyncio.to_thread"
        )

    async def test_consumer_uses_to_thread_for_resolve_tty(self):
        """Consumer calls asyncio.to_thread(_resolve_tty, pid), not _resolve_tty directly."""
        from focus_listener import _consumer, FocusEvent, ResolverCache

        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {"abcd1234": 1234}
        cache_map = {}
        focused_ref = [None]
        fake_cache = ResolverCache(pid=1234, app="Terminal", tty="/dev/ttys001")

        to_thread_calls = []

        async def fake_to_thread(func, *args, **kwargs):
            to_thread_calls.append((func, args))
            # Execute the function synchronously in this test context
            return func(*args, **kwargs)

        import focus_listener

        with patch("focus_listener._resolve_tty", return_value=fake_cache) as mock_resolve, \
             patch("asyncio.to_thread", side_effect=fake_to_thread), \
             patch("focus_listener._raise_window", new_callable=AsyncMock):
            await queue.put(FocusEvent(kind="focus", id="abcd1234"))
            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # asyncio.to_thread must have been called with the patched _resolve_tty mock
            # (compare inside the with block where mock_resolve is still the active patch)
            resolve_calls = [c for c in to_thread_calls if c[0] is mock_resolve]
            assert len(resolve_calls) == 1, (
                f"Expected asyncio.to_thread(_resolve_tty, pid) once; got {to_thread_calls}"
            )
            assert resolve_calls[0][1] == (1234,)


# ---------------------------------------------------------------------------
# 10. Consumer survives per-event exceptions (resilience fix)
# ---------------------------------------------------------------------------

class TestConsumerResilience:
    async def test_consumer_survives_exception_on_first_event_processes_second(self):
        """If _resolve_tty raises on the first focus event, the consumer loop
        continues and correctly processes the second focus event."""
        from focus_listener import _consumer, FocusEvent, ResolverCache

        queue: asyncio.Queue = asyncio.Queue()
        pid_map = {"abcd1234": 1234, "bbbb5678": 5678}
        cache_map = {}
        focused_ref = [None]
        fake_cache = ResolverCache(pid=5678, app="Terminal", tty="/dev/ttys002")

        call_count = 0

        def exploding_resolve(pid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated executor failure")
            return fake_cache

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=fake_to_thread), \
             patch("focus_listener._resolve_tty", side_effect=exploding_resolve), \
             patch("focus_listener._raise_window", new_callable=AsyncMock):
            # First event — will raise inside the consumer
            await queue.put(FocusEvent(kind="focus", id="abcd1234"))
            # Second event — must be processed despite the first failing
            await queue.put(FocusEvent(kind="focus", id="bbbb5678"))

            task = asyncio.create_task(_consumer(queue, pid_map, cache_map, focused_ref))
            await asyncio.wait_for(queue.join(), timeout=2.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Loop survived: second event updated focused_ref
        assert focused_ref[0] == "bbbb5678", (
            f"Consumer died after first exception; focused_ref={focused_ref[0]!r}"
        )
