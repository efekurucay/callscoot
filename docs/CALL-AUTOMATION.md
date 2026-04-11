# Call automation

This document covers the newer automation features in CallScoot:

- active device auto-selection
- incoming-call policy rules
- automatic answer / reject over ADB
- call-session logging

## Active device selection

CallScoot can now work without a permanently pinned Bluetooth MAC or ADB serial.

Resolution order is:

1. explicit CLI `--device`
2. configured `target_device`
3. auto-selection from the active Bluetooth call route
4. learned ADB-to-Bluetooth bindings when exactly one ADB device and one Bluetooth phone are active
5. a single connected Bluetooth phone

When a single ADB phone and a single Bluetooth phone are visible together, CallScoot stores a small binding automatically so future selections are more reliable.

Show the currently resolved targets with:

```bash
callscoot status
```

Relevant fields:

- `selected_target_device`
- `selected_adb_serial`
- `adb_devices_detailed`

## Incoming-call policies

Policies are applied only when `auto_answer` is enabled.

### Modes

- `allow_all`
- `allowlist`
- `blocklist`

### Useful examples

Allow only two numbers:

```bash
callscoot configure \
  --auto-answer on \
  --policy-mode allowlist \
  --allow-caller +905551112233 \
  --allow-caller +905314584141 \
  --unknown-callers deny \
  --auto-reject on
```

Reject one number pattern but allow everything else:

```bash
callscoot configure \
  --policy-mode blocklist \
  --block-caller '*4443322' \
  --auto-reject on
```

Only answer during office hours:

```bash
callscoot configure \
  --business-hours 09:00-18:00 \
  --business-days mon,tue,wed,thu,fri
```

Disable automatic rejection and just let blocked calls keep ringing:

```bash
callscoot configure --auto-reject off
```

## Call logging

Each detected call session is stored under:

```text
~/.local/state/callscoot/calls/<session-id>/
```

Files:

- `meta.json`
- `events.jsonl`
- `transcript.jsonl` (when the AI agent is running)
- `summary.txt` (when the AI agent generates one)

List recent calls:

```bash
callscoot calls
```

Inspect one call:

```bash
callscoot call-show 20260410-223344-+905314584141
```

## Typical automation setup

```bash
callscoot configure \
  --auto-answer on \
  --auto-answer-delay 2 \
  --max-call-duration 600 \
  --policy-mode allow_all \
  --auto-select-device on \
  --log-calls on
```

Then keep the daemon running:

```bash
systemctl --user restart callscoot-daemon.service
callscoot logs -f
```
