"""Integration tests for daemon scan cadence + send-on-change (Task 4 TDD).

Tests the maybe_scan_and_send helper extracted from connect_and_run.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from session_scan import SessionEntry


# ---------------------------------------------------------------------------
# Minimal Session stand-in — mirrors only the attributes we add in Task 4.
# We import maybe_scan_and_send from the daemon module; Session is passed
# in as an argument so we don't need to exercise the full BleakClient wiring.
# ---------------------------------------------------------------------------

def _make_session(last_api_data=None, last_payload_bytes=b"", last_scan_time=0.0):
    """Return a minimal Session-like object with Task-4 state attributes."""
    s = SimpleNamespace(
        last_api_data=last_api_data,
        last_payload_bytes=last_payload_bytes,
        last_scan_time=last_scan_time,
    )

    async def _write_payload(data: bytes) -> bool:
        # patched per test via mock
        raise NotImplementedError("patch write_payload in each test")

    s.write_payload = _write_payload
    return s


# ---------------------------------------------------------------------------
# Import target — pull in after establishing the test objects above so that
# import-time module-level code in the daemon (if any) does not surprise us.
# ---------------------------------------------------------------------------
import claude_usage_daemon as daemon


# ---------------------------------------------------------------------------
# Helper: build a fake SessionEntry
# ---------------------------------------------------------------------------

def _entry(sid="aabbccdd", name="proj", mood="w"):
    return SessionEntry(id=sid, name=name, mood=mood)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScanCadence:
    """Scan cadence: 10 s elapsed → scan_sessions called; <10 s → not called."""

    @pytest.mark.asyncio
    async def test_scan_called_when_interval_elapsed(self):
        """scan_sessions IS called when now - last_scan_time >= SCAN_INTERVAL."""
        session = _make_session(last_api_data={"ok": True, "s": 0, "sr": 0, "w": 0, "wr": 0, "st": "ok"})
        session.last_scan_time = 0.0
        now = daemon.SCAN_INTERVAL + 1  # definitely elapsed

        sessions = []
        focused_ref = [None]
        pid_map = {}

        mock_write = AsyncMock(return_value=True)
        session.write_payload = mock_write

        with patch("claude_usage_daemon.scan_sessions", return_value=[]) as mock_scan:
            await daemon.maybe_scan_and_send(session, sessions, focused_ref, pid_map, now)

        mock_scan.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_scan_not_called_when_interval_not_elapsed(self):
        """scan_sessions is NOT called when < SCAN_INTERVAL has elapsed."""
        session = _make_session(last_api_data={"ok": True, "s": 0, "sr": 0, "w": 0, "wr": 0, "st": "ok"})
        session.last_scan_time = 0.0
        now = daemon.SCAN_INTERVAL - 5  # not yet elapsed

        sessions = []
        focused_ref = [None]
        pid_map = {}

        with patch("claude_usage_daemon.scan_sessions", return_value=[]) as mock_scan:
            await daemon.maybe_scan_and_send(session, sessions, focused_ref, pid_map, now)

        mock_scan.assert_not_called()


class TestSendOnChange:
    """Send-on-change: same scan result twice → write_gatt_char called once, not twice."""

    @pytest.mark.asyncio
    async def test_send_once_on_identical_scan_results(self):
        """Identical payload bytes on second scan → write NOT called again."""
        api_data = {"ok": True, "s": 50, "sr": 10, "w": 30, "wr": 5, "st": "ok"}

        # First call: last_payload_bytes is b"" so payload differs → write happens.
        session = _make_session(last_api_data=api_data)
        sessions = []
        focused_ref = [None]
        pid_map = {}

        mock_write = AsyncMock(return_value=True)
        session.write_payload = mock_write

        now1 = daemon.SCAN_INTERVAL + 1

        with patch("claude_usage_daemon.scan_sessions", return_value=[]):
            await daemon.maybe_scan_and_send(session, sessions, focused_ref, pid_map, now1)

        assert mock_write.call_count == 1
        payload_after_first = session.last_payload_bytes

        # Second call: same scan result, same api_data → payload bytes unchanged → no write.
        session.last_scan_time = 0.0  # reset so interval triggers again
        now2 = now1 + daemon.SCAN_INTERVAL + 1

        with patch("claude_usage_daemon.scan_sessions", return_value=[]):
            await daemon.maybe_scan_and_send(session, sessions, focused_ref, pid_map, now2)

        assert mock_write.call_count == 1  # still 1, not 2
        assert session.last_payload_bytes == payload_after_first


class TestScanErrorGuard:
    """scan_sessions raising → returns previous sessions, no exception, no write."""

    @pytest.mark.asyncio
    async def test_oserror_keeps_previous_sessions(self):
        """OSError from scan_sessions → previous list returned, write_payload not called."""
        api_data = {"ok": True, "s": 10, "sr": 5, "w": 20, "wr": 3, "st": "ok"}
        previous = [_entry("aabbccdd", "proj", "w")]
        session = _make_session(last_api_data=api_data, last_payload_bytes=b"old")
        focused_ref = [None]
        pid_map = {}

        mock_write = AsyncMock(return_value=True)
        session.write_payload = mock_write

        now = daemon.SCAN_INTERVAL + 1

        with patch("claude_usage_daemon.scan_sessions", side_effect=OSError("sessions dir missing")):
            result = await daemon.maybe_scan_and_send(session, list(previous), focused_ref, pid_map, now)

        # Previous list preserved
        assert result == previous
        # No write attempted
        mock_write.assert_not_called()


class TestResponseTrue:
    """Assert write_gatt_char is called with response=True on every RX write."""

    @pytest.mark.asyncio
    async def test_write_payload_uses_response_true(self):
        """Session.write_payload calls write_gatt_char with response=True."""
        from unittest.mock import patch as _patch

        mock_bleak_client = MagicMock()
        mock_bleak_client.write_gatt_char = AsyncMock(return_value=None)

        # Instantiate the real Session class with a mock BleakClient
        s = daemon.Session(mock_bleak_client)

        test_data = b'{"ok":true}'
        await s.write_payload(test_data)

        mock_bleak_client.write_gatt_char.assert_called_once()
        call_kwargs = mock_bleak_client.write_gatt_char.call_args
        # response=True must be passed as keyword argument
        assert call_kwargs.kwargs.get("response") is True
