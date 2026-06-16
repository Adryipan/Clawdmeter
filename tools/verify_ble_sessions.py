#!/usr/bin/env python3
"""E2E BLE verification for multi-session feature.
Usage: python tools/verify_ble_sessions.py [device-name]
"""
import asyncio, json, sys
from bleak import BleakClient, BleakScanner

DEVICE_NAME    = sys.argv[1] if len(sys.argv) > 1 else "Clawdmeter"
FOCUS_CHAR_UUID = "4c41555a-4465-7669-6365-000000000005"


async def main():
    print(f"Scanning for {DEVICE_NAME}...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10)
    if not device:
        print(f"FAIL: device '{DEVICE_NAME}' not found")
        sys.exit(1)

    focus_events = []
    focus_received = asyncio.Event()

    def on_focus(_char, data: bytearray):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as e:
            print(f"FAIL: FOCUS_CHAR payload not valid JSON: {bytes(data)!r} ({e})")
            focus_events.append({"_error": str(e)})
            focus_received.set()
            return
        focus_events.append(obj)
        print(f"  FOCUS notify: {obj}")
        focus_received.set()

    async with BleakClient(device) as client:
        print("Connected.")
        await client.start_notify(FOCUS_CHAR_UUID, on_focus)
        print("Subscribed to FOCUS_CHAR — waiting for initial notify...")
        try:
            await asyncio.wait_for(focus_received.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            print("FAIL: no initial FOCUS_CHAR notification within 5 s")
            sys.exit(1)

        if not focus_events:
            print("FAIL: no focus events received")
            sys.exit(1)
        first = focus_events[0]
        if "_error" in first:
            print(f"FAIL: FOCUS_CHAR payload parse error: {first['_error']}")
            sys.exit(1)
        if "focus" not in first and "btn" not in first:
            print(f"FAIL: unexpected FOCUS_CHAR payload shape: {first}")
            sys.exit(1)
        print(f"PASS: initial FOCUS_CHAR notify shape OK: {first}")
        print("PASS: all assertions passed.")


asyncio.run(main())
