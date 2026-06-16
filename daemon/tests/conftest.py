from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(scope="session", autouse=True)
def _ensure_fixture_dirs():
    for name in ("live", "stale", "dead", "malformed", "reuse", "nine_sessions"):
        (Path(__file__).parent / "fixtures" / name).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_bleak_client():
    client = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    return client
