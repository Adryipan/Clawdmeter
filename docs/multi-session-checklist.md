On-device verification checklist for the multi-session feature (feat/multi-session). Flash, then walk top to bottom.

## Prerequisites

The following checklist items require the daemon running:
- "Daemon log shows window-raise attempt…" (row tap)
- "Reconnect: daemon re-subscribes…"
- "GPIO18 tap, focus + daemon sub…"

Start daemon and tail logs:

```bash
launchctl start com.user.claude-usage-daemon
tail -F ~/Library/Logs/claude-usage-daemon.out.log
```

Items marked *(no daemon)* work without it. All others assume daemon connected.

- [ ] Flash: pio run -d firmware -e waveshare_amoled_216 --target upload exits 0
- [ ] 0 sessions: sessions screen shows "no sessions"; splash behaves as today
- [ ] 1 session idle: sessions screen shows 1 row with sleep sprite
- [ ] 4 sessions: all 4 rows visible, no scroll
- [ ] 8 sessions: scroll indicator visible
- [ ] Row tap: focused row highlights orange; auto-returns to usage in ~600 ms
- [ ] Daemon log shows window-raise attempt within 300 ms of row tap
- [ ] Header tap cycles splash → usage → sessions → splash
- [ ] Row tap does NOT cycle screen (no event bubble)
- [ ] Splash badge appears naming mood-owning session ("training-planner needs you")
- [ ] asking session overrides sleeping on splash
- [ ] Zero sessions after all PIDs exit: badge hidden, splash reverts to rate-group
- [ ] Old daemon (no "ss"): usage screen works, sessions screen shows "no sessions"
- [ ] Reconnect: daemon re-subscribes; device notifies current focus
- [ ] GPIO0 hold (3 s PTT): full Space duration held; no synthetic release
- [ ] GPIO18 tap, focus + daemon sub: single Shift+Tab lands in focused terminal
- [ ] GPIO18 tap, no subscriber: immediate Shift+Tab (edge-based, no 300 ms wait)
- [ ] GPIO18 second press during pending tap: ignored (single-shot latch)
- [ ] verify_ble_sessions.py exits 0 (daemon not required)

## Flash

Flash: pio run -d firmware -e waveshare_amoled_216 --target upload
Expected: ends with "Leaving... Hard resetting via RTS pin..."
