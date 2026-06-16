"""Tests for daemon/payload.py — build_payload, field caps, truncation ladder."""
import json
import sys
from pathlib import Path
import pytest

# Make daemon/ importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from session_scan import SessionEntry, MOOD_WORKING, MOOD_ASKING, MOOD_SLEEPING, MOOD_DEAD

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

API_DATA = {"s": 42.5, "sr": "2h 15m", "w": 12.0, "wr": "5d 3h", "st": 1, "ok": 1}

PAYLOAD_BUDGET = 480


def make_session(
    sid: str = "abcd1234",
    name: str = "myproject",
    mood: str = MOOD_WORKING,
    detail: str = "",
) -> SessionEntry:
    return SessionEntry(id=sid, name=name, mood=mood, detail=detail)


def _parse(result: bytes) -> dict:
    return json.loads(result.decode())


# ---------------------------------------------------------------------------
# 1. Basic structure: flat api_data fields pass through + ss array present
# ---------------------------------------------------------------------------

def test_build_payload_passthrough_and_ss():
    from payload import build_payload

    sessions = [make_session()]
    result = _parse(build_payload(API_DATA, sessions))

    # Existing flat fields pass through unchanged
    for key, val in API_DATA.items():
        assert result[key] == val, f"flat field {key!r} missing or changed"

    # ss array present with one entry
    assert "ss" in result
    assert len(result["ss"]) == 1


# ---------------------------------------------------------------------------
# 2. Field caps: i ≤ 8, n ≤ 16 bytes, d ≤ 16 bytes
# ---------------------------------------------------------------------------

def test_field_caps_enforced():
    from payload import build_payload

    long_session = SessionEntry(
        id="a" * 20,    # 20 chars → truncated to 8
        name="b" * 30,  # 30 ASCII chars → truncated to 16 bytes
        mood=MOOD_WORKING,
        detail="c" * 30,  # 30 ASCII chars → truncated to 16 bytes
    )
    result = _parse(build_payload(API_DATA, [long_session]))

    entry = result["ss"][0]
    assert len(entry["i"]) == 8, f"id not capped to 8: got {len(entry['i'])}"
    assert len(entry["n"].encode("utf-8")) <= 16, f"name not capped to 16 bytes: got {len(entry['n'].encode('utf-8'))}"
    assert len(entry["d"].encode("utf-8")) <= 16, f"detail not capped to 16 bytes: got {len(entry['d'].encode('utf-8'))}"


def test_field_caps_multibyte_utf8():
    """Non-ASCII names must be capped to ≤16 UTF-8 BYTES, not ≤16 code points.

    'é' is 2 bytes in UTF-8.  20×'é' = 40 bytes; after _cap_bytes(16) → 8 chars / 16 bytes.
    The JSON round-trip must also succeed (no UnicodeDecodeError from split codepoints).
    """
    from payload import build_payload

    multibyte_session = SessionEntry(
        id="abcd1234",
        name="é" * 20,    # 20 chars, 40 UTF-8 bytes
        mood=MOOD_WORKING,
        detail="ñ" * 20,  # 20 chars, 40 UTF-8 bytes
    )
    result = _parse(build_payload(API_DATA, [multibyte_session]))

    entry = result["ss"][0]
    # Byte-length caps enforced
    assert len(entry["n"].encode("utf-8")) <= 16, (
        f"name exceeds 16 UTF-8 bytes: {len(entry['n'].encode('utf-8'))} bytes"
    )
    assert len(entry["d"].encode("utf-8")) <= 16, (
        f"detail exceeds 16 UTF-8 bytes: {len(entry['d'].encode('utf-8'))} bytes"
    )
    # Must be valid UTF-8 (json.loads above already proved this, but be explicit)
    assert entry["n"].encode("utf-8").decode("utf-8") == entry["n"]
    assert entry["d"].encode("utf-8").decode("utf-8") == entry["d"]


# ---------------------------------------------------------------------------
# 3 & 4. Two-sided budget test: pre-ladder >480 B, post-ladder ≤480 B
# ---------------------------------------------------------------------------

def _make_maximal_sessions(count: int = 8) -> list[SessionEntry]:
    """8-char id, 16-char name, 16-char detail, distinct per entry."""
    sessions = []
    for i in range(count):
        sessions.append(SessionEntry(
            id=f"sess{i:04d}",          # exactly 8 chars
            name=f"project-{i:07d}",   # exactly 16 chars
            mood=MOOD_WORKING,
            detail=f"detail--{i:07d}",  # exactly 16 chars
        ))
    return sessions


def test_maximal_sessions_pre_ladder_exceeds_budget():
    """Verify the 8-maximal-session payload is actually >480 B before truncation."""
    sessions = _make_maximal_sessions(8)
    # Build what payload.py would build WITHOUT the ladder (manual construction)
    ss = []
    for s in sessions:
        ss.append({"i": s.id[:8], "n": s.name[:16], "m": s.mood, "d": s.detail[:16]})
    raw = dict(API_DATA)
    raw["ss"] = ss
    pre_ladder_bytes = json.dumps(raw, separators=(",", ":")).encode()
    assert len(pre_ladder_bytes) > PAYLOAD_BUDGET, (
        f"Test setup invalid: pre-ladder size {len(pre_ladder_bytes)} B is not >480 B; "
        "adjust maximal fixture so ladder actually fires."
    )


def test_maximal_sessions_post_ladder_within_budget():
    """build_payload on 8 maximal sessions must return ≤480 B."""
    from payload import build_payload

    sessions = _make_maximal_sessions(8)
    result = build_payload(API_DATA, sessions)
    assert len(result) <= PAYLOAD_BUDGET, (
        f"Post-ladder payload {len(result)} B exceeds budget {PAYLOAD_BUDGET} B"
    )


# ---------------------------------------------------------------------------
# 5. Ladder step 1: dropping 'd' fields alone gets under budget
# ---------------------------------------------------------------------------

def test_ladder_step1_drops_d_fields():
    """Craft a payload where dropping 'd' brings it under budget but no entries dropped."""
    from payload import build_payload

    sessions = _make_maximal_sessions(8)
    result = _parse(build_payload(API_DATA, sessions))

    # After step 1 (drop d), all 8 entries survive — no entries were removed
    assert len(result["ss"]) == 8, (
        f"Expected 8 entries after step-1 ladder, got {len(result['ss'])}"
    )
    # No entry should have a 'd' key (step 1 removed them all)
    for entry in result["ss"]:
        assert "d" not in entry, f"'d' field survived ladder step 1: {entry}"


# ---------------------------------------------------------------------------
# 6. Ladder step 2: tail entries dropped when step 1 not enough
# ---------------------------------------------------------------------------

def test_ladder_step2_drops_tail_not_head():
    """Step 2 drops tail entries when step 1 (no 'd' to drop) isn't sufficient.

    Uses _PADDED_API so the no-detail payload deterministically exceeds 480 B
    after step 1 is a no-op, forcing step 2 unconditionally.
    """
    from payload import build_payload

    # 8 sessions, NO detail — step 1 is a no-op
    sessions = [
        SessionEntry(id=f"sess{i:04d}", name=f"project-{i:07d}", mood=MOOD_WORKING)
        for i in range(8)
    ]

    # Guard: confirm step 2 is actually required (no-d payload must exceed budget)
    ss_no_d = [{"i": s.id[:8], "n": s.name[:16], "m": s.mood} for s in sessions]
    pre = dict(_PADDED_API)
    pre["ss"] = ss_no_d
    pre_size = len(json.dumps(pre, separators=(",", ":")).encode())
    assert pre_size > PAYLOAD_BUDGET, (
        f"Test setup invalid: no-d payload {pre_size} B ≤ 480 B; step 2 won't fire."
    )

    result_bytes = build_payload(_PADDED_API, sessions)
    result = _parse(result_bytes)
    ids_in_result = [e["i"] for e in result["ss"]]

    assert len(result_bytes) <= PAYLOAD_BUDGET, (
        f"Post-ladder payload {len(result_bytes)} B exceeds budget"
    )
    assert len(result["ss"]) < 8, "Expected step 2 to drop tail entries"
    assert "sess0000" in ids_in_result, "Head entry was dropped instead of a tail entry"


# ---------------------------------------------------------------------------
# 7. Focused session never dropped by step 2  (step 2 MUST actually fire)
# ---------------------------------------------------------------------------

# API_DATA padded so that 8 no-detail sessions still exceed 480 B.
# Measured: post-step1 with plain API_DATA = 443 B (≤480, step 2 never fires).
# Adding 38 chars of padding pushes the no-d payload to ~490 B, forcing step 2.
_PADDED_API = {**API_DATA, "pad": "x" * 38}


def test_focused_session_survives_ladder():
    """Focused session at TAIL must not be dropped by step 2.

    Construction guarantees step 2 executes:
    - Sessions have no 'd' fields → step 1 is a no-op (nothing to drop).
    - Padded api_data pushes the 8-entry no-d payload over 480 B.
    - Step 2 must drop unfocused tail entries to reach budget.
    """
    from payload import build_payload

    # 8 sessions, NO detail (step 1 does nothing), focused pinned at TAIL
    sessions = [
        SessionEntry(id=f"sess{i:04d}", name=f"project-{i:07d}", mood=MOOD_WORKING)
        for i in range(8)
    ]
    focused_id = sessions[-1].id  # "sess0007" — tail position

    # Verify step 2 will be forced: no-d payload with padded api must exceed budget
    ss_no_d = [{"i": s.id[:8], "n": s.name[:16], "m": s.mood} for s in sessions]
    pre_step2 = dict(_PADDED_API)
    pre_step2["ss"] = ss_no_d
    pre_step2_size = len(json.dumps(pre_step2, separators=(",", ":")).encode())
    assert pre_step2_size > PAYLOAD_BUDGET, (
        f"Test setup invalid: no-d payload {pre_step2_size} B is not >480 B; "
        "step 2 won't fire. Increase padding."
    )

    result_bytes = build_payload(_PADDED_API, sessions, focused_id=focused_id)
    result = _parse(result_bytes)

    ids_in_result = [e["i"] for e in result["ss"]]

    # (a) output fits budget
    assert len(result_bytes) <= PAYLOAD_BUDGET, (
        f"Post-ladder payload {len(result_bytes)} B exceeds budget"
    )
    # (b) focused entry present with f:1
    assert focused_id in ids_in_result, (
        f"Focused session {focused_id!r} was dropped; survivors: {ids_in_result}"
    )
    focused_entries = [e for e in result["ss"] if e["i"] == focused_id]
    assert focused_entries[0].get("f") == 1, "f:1 missing on focused entry"

    # (c) at least one unfocused entry was dropped (confirms step 2 ran)
    assert len(result["ss"]) < 8, (
        f"Expected step 2 to drop tail entries; got all 8 back. "
        f"pre_step2_size={pre_step2_size} B — padding may be insufficient."
    )

    # (d) dropped entries came from the tail: head unfocused entries survive in order
    # sess0000 is the head (first, highest-priority unfocused) and must be present
    assert "sess0000" in ids_in_result, (
        f"Head entry 'sess0000' was dropped instead of a tail entry; "
        f"survivors: {ids_in_result}"
    )


# ---------------------------------------------------------------------------
# 8. No ss key when sessions is empty → firmware fallback
# ---------------------------------------------------------------------------

def test_empty_sessions_no_ss_key():
    from payload import build_payload

    result = _parse(build_payload(API_DATA, []))
    assert "ss" not in result, f"Expected no 'ss' key for empty sessions; got {result}"

    # Flat fields still present
    for key in API_DATA:
        assert key in result


# ---------------------------------------------------------------------------
# 9. f:1 on focused entry only, absent on all others
# ---------------------------------------------------------------------------

def test_focused_flag_on_correct_entry_only():
    from payload import build_payload

    sessions = [
        make_session(sid="aaaa0001", name="alpha"),
        make_session(sid="bbbb0002", name="beta"),
        make_session(sid="cccc0003", name="gamma"),
    ]
    focused_id = "bbbb0002"

    result = _parse(build_payload(API_DATA, sessions, focused_id=focused_id))

    for entry in result["ss"]:
        if entry["i"] == focused_id:
            assert entry.get("f") == 1, f"Expected f:1 on focused entry; got {entry}"
        else:
            assert "f" not in entry, f"Unexpected f key on non-focused entry: {entry}"


# ---------------------------------------------------------------------------
# 10. Result is valid bytes (not str)
# ---------------------------------------------------------------------------

def test_build_payload_returns_bytes():
    from payload import build_payload

    result = build_payload(API_DATA, [make_session()])
    assert isinstance(result, bytes), f"Expected bytes, got {type(result)}"
