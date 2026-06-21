"""Tests for the heartbeat watchdog (TDD Red phase — should FAIL before implementation).

Tests: send_watchdog fires on stale last_send_time, does not fire when fresh,
and write_payload updates last_send_time on success.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import claude_usage_daemon as daemon
from claude_usage_daemon import send_watchdog, WATCHDOG_TIMEOUT, Session


class TestWatchdogFiresWhenStale:
    """test_watchdog_fires_when_stale: disconnect called when last_send_time is long past threshold."""

    @pytest.mark.asyncio
    async def test_watchdog_fires_when_stale(self, monkeypatch):
        """Watchdog calls client.disconnect() exactly once when no send in > WATCHDOG_TIMEOUT seconds."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()

        session = Session(mock_client)
        # Force stale: last send was 10,000 seconds ago
        session.last_send_time = time.time() - 10_000

        stop_event = asyncio.Event()

        # Patch so watchdog fires immediately (1s timeout, 0.05s tick)
        monkeypatch.setattr(daemon, "WATCHDOG_TIMEOUT", 1)
        monkeypatch.setattr(daemon, "TICK", 0.05)

        # Watchdog should fire and return (disconnect aborts the loop)
        await asyncio.wait_for(send_watchdog(mock_client, session, stop_event), timeout=2)

        mock_client.disconnect.assert_awaited_once()


class TestWatchdogDoesNotFireWhenFresh:
    """test_watchdog_does_not_fire_when_fresh: disconnect NOT called when last_send_time is recent."""

    @pytest.mark.asyncio
    async def test_watchdog_does_not_fire_when_fresh(self, monkeypatch):
        """Watchdog does NOT call disconnect when last_send_time is recent (within threshold)."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()

        session = Session(mock_client)
        # Recent send: just now
        session.last_send_time = time.time()

        stop_event = asyncio.Event()

        # Use a long WATCHDOG_TIMEOUT so it won't fire in our short run window
        monkeypatch.setattr(daemon, "WATCHDOG_TIMEOUT", 9999)
        monkeypatch.setattr(daemon, "TICK", 0.05)

        # Run for a short bounded time then cancel — disconnect must NOT have been called
        task = asyncio.create_task(send_watchdog(mock_client, session, stop_event))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        mock_client.disconnect.assert_not_called()


class TestWatchdogDisconnectExceptionIsSwallowed:
    """test_watchdog_disconnect_exception_is_swallowed: send_watchdog completes without raising when disconnect() throws."""

    @pytest.mark.asyncio
    async def test_watchdog_disconnect_exception_is_swallowed(self, monkeypatch):
        """send_watchdog does not propagate exceptions from client.disconnect()."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock(side_effect=Exception("BLE gone"))

        session = Session(mock_client)
        # Force stale: last send was 10,000 seconds ago
        session.last_send_time = time.time() - 10_000

        stop_event = asyncio.Event()

        monkeypatch.setattr(daemon, "WATCHDOG_TIMEOUT", 1)
        monkeypatch.setattr(daemon, "TICK", 0.05)

        # Must complete without raising even though disconnect() throws
        await asyncio.wait_for(send_watchdog(mock_client, session, stop_event), timeout=2)

        mock_client.disconnect.assert_awaited_once()


class TestWritePayloadUpdatesLastSendTime:
    """test_write_payload_updates_last_send_time: Session.write_payload updates last_send_time on success."""

    @pytest.mark.asyncio
    async def test_write_payload_updates_last_send_time(self):
        """write_payload updates session.last_send_time after a successful write_gatt_char."""
        mock_client = MagicMock()
        mock_client.write_gatt_char = AsyncMock(return_value=None)

        session = Session(mock_client)

        # Record timestamp before write
        before = session.last_send_time

        # Small sleep to ensure time advances measurably
        await asyncio.sleep(0.01)

        result = await session.write_payload(b"test")

        assert result is True
        assert session.last_send_time >= before
        mock_client.write_gatt_char.assert_awaited_once()
