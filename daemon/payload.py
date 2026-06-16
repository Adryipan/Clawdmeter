"""Build BLE JSON payload from API data + session list. ≤480 B post-ladder."""
import json
from typing import Optional
from session_scan import SessionEntry

PAYLOAD_BUDGET = 480


def _serialize(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _cap_bytes(s: str, max_bytes: int) -> str:
    """Cap s to at most max_bytes UTF-8 bytes, dropping any split trailing codepoint."""
    b = s.encode("utf-8")[:max_bytes]
    return b.decode("utf-8", errors="ignore")


def _build_ss(sessions: list[SessionEntry], focused_id: Optional[str]) -> list[dict]:
    result = []
    for s in sessions:
        entry: dict = {
            "i": s.id[:8],  # session ids are ASCII hex by construction
            "n": _cap_bytes(s.name, 16),   # firmware buffer is char[17] bytes
            "m": s.mood,
        }
        if s.detail:
            entry["d"] = _cap_bytes(s.detail, 16)  # firmware buffer is char[17] bytes
        if focused_id and s.id == focused_id:
            entry["f"] = 1
        result.append(entry)
    return result


def build_payload(
    api_data: dict,
    sessions: list[SessionEntry],
    focused_id: Optional[str] = None,
) -> bytes:
    """Return serialized payload bytes, ≤PAYLOAD_BUDGET after truncation ladder."""
    payload = dict(api_data)  # s, sr, w, wr, st, ok

    if not sessions:
        return _serialize(payload)

    ss = _build_ss(sessions, focused_id)
    payload["ss"] = ss

    data = _serialize(payload)
    if len(data) <= PAYLOAD_BUDGET:
        return data

    # Step 1: drop all 'd' fields
    for entry in ss:
        entry.pop("d", None)
    # ss is the same object already in payload["ss"] — no reassignment needed
    data = _serialize(payload)
    if len(data) <= PAYLOAD_BUDGET:
        return data

    # Step 2: drop tail entries, never the focused entry.
    # O(n²) but n ≤ MAX_SESSIONS=8 by contract — acceptable.
    while len(ss) > 1 and len(data) > PAYLOAD_BUDGET:
        for i in range(len(ss) - 1, -1, -1):
            if not ss[i].get("f"):
                ss.pop(i)
                break
        else:
            break  # only focused entry left, stop
        # ss is the same object already in payload["ss"] — no reassignment needed
        data = _serialize(payload)

    return data
