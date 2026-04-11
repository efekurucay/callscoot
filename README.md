<p align="center">
  <img src="assets/banner.svg" alt="CallScoot Banner" width="100%"/>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"/></a>
  <img src="https://img.shields.io/badge/Platform-Linux-blue?logo=linux&logoColor=white" alt="Platform: Linux"/>
  <img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white" alt="Python 3.8+"/>
  <img src="https://img.shields.io/badge/Bluetooth-HFP%20%7C%20HSP-0082FC?logo=bluetooth&logoColor=white" alt="Bluetooth HFP/HSP"/>
  <img src="https://img.shields.io/badge/Audio-PipeWire-red?logo=linux&logoColor=white" alt="PipeWire"/>
  <img src="https://img.shields.io/badge/ADB-Optional-green?logo=android&logoColor=white" alt="ADB Optional"/>
  <img src="https://img.shields.io/badge/Voice%20Agent-ElevenAgents-blueviolet" alt="Voice Agent"/>
  <img src="https://img.shields.io/badge/Headless-Systemd%20Service-lightgrey?logo=systemd&logoColor=white" alt="Systemd Service"/>
</p>

# CallScoot

Turn a Linux laptop into a Bluetooth call deck for an Android phone.

CallScoot makes the laptop behave like a local call console:

- Android phone sends call audio to the laptop over **Bluetooth HFP/HSP**
- laptop microphone audio goes back to the phone
- laptop speakers play the remote caller
- optional **echo cancellation** is enabled by default
- optional **ADB helpers** can dial / answer / hang up Android calls
- optional **SIP telephony mode** can register to a SIP server and place calls directly
- optional **call policies** can auto-answer / auto-reject incoming calls
- optional **real-time voice agent mode** can run inside the call
- local HTTP API makes it easy for another app to use CallScoot as a call runtime
- runs as a **headless user service** on an always-on Linux box

This project is Linux-first and tested against:

- Debian 13 / BlueZ 5.82
- PipeWire 1.4.x
- WirePlumber 0.5.x
- Android phone over Bluetooth HFP/HSP

See also:

- [`docs/WHAT-THIS-REPO-DOES-TODAY.md`](docs/WHAT-THIS-REPO-DOES-TODAY.md)
- [`docs/CALL-AUTOMATION.md`](docs/CALL-AUTOMATION.md)
- [`docs/AI-AGENT-INTEGRATION.md`](docs/AI-AGENT-INTEGRATION.md)
- [`docs/AI-VOICE-ROUTING.md`](docs/AI-VOICE-ROUTING.md)
- [`docs/AI-AGENT-RUNBOOK.md`](docs/AI-AGENT-RUNBOOK.md)
- [`docs/API.md`](docs/API.md)
- [`docs/SIP.md`](docs/SIP.md)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- [`examples/lead_campaign_app.py`](examples/lead_campaign_app.py)
- [`examples/minimal_client_app.py`](examples/minimal_client_app.py)

---

## What this repo does right now

As shipped today, CallScoot is primarily a **Linux-side Bluetooth call audio bridge** with an optional SIP telephony backend.

It is useful when all of these are true:

- your Android phone is paired to the laptop over Bluetooth
- Android is allowed to use the laptop for **call audio**
- Linux exposes the phone as an HFP/HSP Bluetooth audio device
- you want the **laptop microphone + laptop speakers** to be the active call endpoint

When those conditions are met, CallScoot does this automatically:

1. watches PipeWire / BlueZ for a live Bluetooth **HFP/HSP source + sink** pair
2. switches the Bluetooth card to a headset / hands-free profile when possible
3. creates an audio graph so that:
   - phone call audio plays on the laptop speakers
   - laptop mic audio goes back to the phone
4. optionally inserts echo cancellation in the middle
5. removes the bridge again when the Bluetooth call route disappears

In short:

> This repo turns a Linux laptop into a local Bluetooth speakerphone / headset bridge for Android calls.

---

## What this repo does not try to do

This repository intentionally does **not** do these things yet:

- no Android companion app
- no contact sync
- no voice command parser like "call Ahmet"
- no custom Bluetooth telephony stack
- no raw phone-call audio capture over ADB
- no GUI

Optional ADB commands are included only as convenience helpers for dialing / answering / hanging up.
The actual call audio path is still Bluetooth HFP/HSP.

---

## Why this exists

The goal is simple:

> Keep an old laptop on at home, pair it with your Android phone, and use the laptop's mic + speakers as the call endpoint.

No cloud service. No vendor lock-in. No proprietary phone desktop suite.

---

## How it works

CallScoot does **not** try to capture protected phone-call audio over ADB.
Instead it uses the standard path that car kits and headsets use:

```text
Android call audio <-> Bluetooth HFP/HSP <-> Linux laptop
```

Then on Linux it builds this PipeWire/PulseAudio graph:

```text
phone BT source  -> echo-cancel sink -> laptop speakers
laptop mic/AEC   -> loopback         -> phone BT sink
```

So the phone keeps the actual cellular/VoIP call, while the laptop becomes the live microphone/speaker endpoint.

---

## Features

- Auto-detects Bluetooth HFP/HSP device pairs and stream-based call routes
- Auto-switches BlueZ cards to an HFP profile when possible
- Echo cancellation via `module-echo-cancel`
- Two loopbacks via `module-loopback`
- Headless-friendly WirePlumber config (`seat-monitoring = disabled`)
- Systemd user service for always-on usage
- Optional ADB call helpers, active-device auto-selection, and auto-answer support
- Optional SIP backend using `pjsua2` with configurable server / username / password / transport
- Incoming-call policy engine: allowlist / blocklist / business hours
- Per-call session logs under `~/.local/state/callscoot/calls/`
- ElevenAgents real-time voice agent mode
- Local HTTP API for outbound call orchestration and event streaming
- Zero-dependency Python client helper for external apps (`src/callscoot_client.py`)
- Optional `callscoot-agent` pipeline with OpenAI / Ollama / whisper-cli / espeak / mock providers

---

## What each command is for

| Command | What it does |
|---|---|
| `callscoot pair` | Opens a temporary Bluetooth pairing window on the laptop |
| `callscoot devices` | Lists paired Bluetooth devices |
| `callscoot trust MAC` | Marks a paired device as trusted in BlueZ |
| `callscoot connect MAC` | Tries to connect the paired Bluetooth device |
| `callscoot configure ...` | Saves the target device / local audio / latency / ADB or SIP preferences |
| `callscoot up` | Builds the audio bridge immediately if a phone HFP/HSP route exists |
| `callscoot down` | Removes the bridge modules |
| `callscoot daemon` | Runs the background watcher that auto-builds the bridge |
| `callscoot status` | Prints config, selected devices, BlueZ devices, PipeWire route info, service state |
| `callscoot logs -f` | Follows daemon logs |
| `callscoot calls` | Lists recent call sessions |
| `callscoot call-show ID` | Shows one call session including transcript if present |
| `callscoot dial NUMBER` | Starts a call through the selected telephony backend (`adb` or `sip`) |
| `callscoot answer` | Answers through the selected telephony backend |
| `callscoot hangup` | Hangs up through the selected telephony backend |
| `callscoot-agent bootstrap-audio` | Creates the AI virtual sinks and points CallScoot at them |
| `callscoot-agent run` | Runs the AI call agent |
| `callscoot-api` | Runs the local HTTP API for external apps |

---

## Repository layout

```text
bin/callscoot                 launcher
src/callscoot.py             main CLI + daemon
src/callscoot_agent.py       AI call agent launcher
src/callscoot_api.py         local HTTP API for external apps
src/callscoot_client.py      zero-dependency Python client helper
src/sip_backend.py           optional SIP telephony backend
src/agent_orchestrator.py    ElevenAgents runtime
src/audio_bridge.py          PulseAudio capture/playback bridge
src/agent_events.py          structured per-call event log writer
src/agent_memory.py          local SQLite memory/profile store
src/agent_control.py         pending call requests + command queue
scripts/install-system.sh    root/system package setup
scripts/install-user.sh      user install + systemd services
systemd/callscoot-daemon.service
systemd/callscoot-agent.service
systemd/callscoot-api.service
config/10-callscoot-bluetooth.conf
```

---

## Recommended deployment shape

CallScoot is meant to run as a **headless runtime** on the Linux machine that is paired to the Android phone.

Your own app should sit on top of the local API.

```text
client app
   -> CallScoot local API
   -> CallScoot runtime services
   -> ElevenAgents
   -> Android phone over Bluetooth HFP/HSP
```

Recommended default:

- run CallScoot and your client app on the **same Linux machine**
- let the client app call `http://127.0.0.1:8788`
- keep UI / business logic in the client app
- keep Bluetooth / telephony / session lifecycle in CallScoot

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full deployment model.

---

## Install

### 1) System packages

```bash
sudo ./scripts/install-system.sh "$USER"
```

This installs:

- BlueZ
- PipeWire
- WirePlumber
- Bluetooth SPA plugins
- PulseAudio compatibility tools
- ADB
- jq
- espeak-ng

It also enables `bluetooth.service` and `loginctl enable-linger` for the target user.

### 2) User service

```bash
./scripts/install-user.sh
```

This installs:

- `~/.local/bin/callscoot`
- `~/.local/bin/callscoot-agent`
- `~/.local/bin/callscoot-api`
- `~/.local/lib/callscoot/*.py`
- `~/.config/systemd/user/callscoot-daemon.service`
- `~/.config/systemd/user/callscoot-agent.service`
- `~/.config/systemd/user/callscoot-api.service`
- `~/.config/wireplumber/wireplumber.conf.d/10-callscoot-bluetooth.conf`

And it restarts PipeWire/WirePlumber and enables:

- `callscoot-daemon.service`
- `callscoot-agent.service`
- `callscoot-api.service`

---

## First pairing

Open a pairing window on the laptop:

```bash
callscoot pair
```

Then on the phone:

1. Open Bluetooth settings
2. Pair with the laptop
3. Trust / allow call audio on Android if asked

After pairing:

```bash
callscoot devices
```

If you want to pin CallScoot to one specific phone:

```bash
callscoot configure --device AA:BB:CC:DD:EE:FF
```

You can also trust and reconnect manually:

```bash
callscoot trust AA:BB:CC:DD:EE:FF
callscoot connect AA:BB:CC:DD:EE:FF
```

---

## Usage

### Automatic mode

The daemon is the normal mode:

```bash
systemctl --user status callscoot-daemon.service
callscoot logs -f
```

### Local API for your own app

CallScoot can be used as a local call runtime by another application.

Start / inspect the API service:

```bash
systemctl --user status callscoot-api.service
curl http://127.0.0.1:8788/v1/health
```

Queue an outbound call with injected call context:

```bash
curl -X POST http://127.0.0.1:8788/v1/outbound-calls \
  -H 'Content-Type: application/json' \
  -d '{
    "number": "+905551112233",
    "dynamic_variables": {
      "campaign_name": "lead_qualification",
      "contact_name": "Efe"
    },
    "metadata": {
      "lead_id": "lead-123"
    }
  }'
```

Stream structured events from the active session:

```bash
curl -N 'http://127.0.0.1:8788/v1/events/stream?session_id=current'
```

See [`docs/API.md`](docs/API.md) for the full external-app integration flow.

If you are writing a Python client app, start from:

- `src/callscoot_client.py`
- `examples/minimal_client_app.py`
- `examples/lead_campaign_app.py`

When the Android phone exposes an HFP/HSP audio route, CallScoot automatically builds the bridge.

### Manual mode

```bash
callscoot up
callscoot down
```

If you want to target a specific phone manually:

```bash
callscoot up --device AA:BB:CC:DD:EE:FF
```

---

## Configuration

Show current config:

```bash
callscoot configure
```

Examples:

```bash
callscoot configure --device AA:BB:CC:DD:EE:FF
callscoot configure --echo-cancel on
callscoot configure --latency 40
callscoot configure --sink alsa_output.pci-0000_00_1f.3.analog-stereo
callscoot configure --source alsa_input.pci-0000_00_1f.3.analog-stereo
callscoot configure --adb-serial 192.168.1.50:5555
callscoot configure --auto-answer on
callscoot configure --auto-answer-delay 2
callscoot configure --max-call-duration 600
callscoot configure --auto-select-device on
callscoot configure --policy-mode allowlist --allow-caller +905551112233 --unknown-callers deny --auto-reject on
callscoot configure --business-hours 09:00-18:00 --business-days mon,tue,wed,thu,fri
```

Clear pinned values:

```bash
callscoot configure --clear-device
callscoot configure --clear-adb-serial
```

### SIP telephony mode

```bash
callscoot configure \
  --telephony-backend sip \
  --sip-server sip.example.com \
  --sip-username 1001 \
  --sip-password supersecret \
  --sip-port 5060 \
  --sip-transport udp \
  --sip-audio-mode direct
```

Optional SIP audio device overrides (direct mode):

```bash
callscoot configure --sip-playback-device pipewire
callscoot configure --sip-capture-device pipewire
```

Enable SIP + AI agent routing:

```bash
callscoot configure --sip-audio-mode agent
systemctl --user restart callscoot-api.service callscoot-agent.service
```

Remove SIP settings and switch back to Android/ADB mode:

```bash
callscoot configure --clear-sip
```

See [`docs/SIP.md`](docs/SIP.md) for details.

---

## Optional ADB helpers

If your Android device is already reachable with ADB:

```bash
callscoot dial +905551112233
callscoot answer
callscoot hangup
callscoot configure --auto-answer on
```

You can also delay auto-answer slightly so Bluetooth audio has time to settle:

```bash
callscoot configure --auto-answer-delay 2
```

If you want long calls to be cut off automatically, set a maximum call duration in seconds:

```bash
callscoot configure --max-call-duration 600
```

Use `0` to disable the limit.

And you can apply call policies:

```bash
callscoot configure --policy-mode blocklist --block-caller '*4443322' --auto-reject on
```

These are convenience helpers only.
The actual audio path for calls should still be Bluetooth HFP/HSP.

---

## Call automation

For an always-on desk-phone style setup:

```bash
callscoot configure \
  --auto-select-device on \
  --auto-answer on \
  --auto-answer-delay 2 \
  --max-call-duration 600 \
  --policy-mode allow_all \
  --log-calls on
systemctl --user restart callscoot-daemon.service
```

More advanced examples are in [`docs/CALL-AUTOMATION.md`](docs/CALL-AUTOMATION.md).

---

## Optional AI agent

Create the AI virtual sinks:

```bash
callscoot-agent bootstrap-audio
systemctl --user restart callscoot-daemon.service
```

Configure a provider stack, for example:

```bash
callscoot-agent configure \
  --stt-provider mock \
  --llm-provider mock \
  --tts-provider mock
```

Smoke-test the pipeline:

```bash
callscoot-agent reply "Merhaba"
```

Run the continuous agent loop:

```bash
callscoot-agent run
```

For provider details, see [`docs/AI-AGENT-RUNBOOK.md`](docs/AI-AGENT-RUNBOOK.md).

---

## Status / debugging

```bash
callscoot status
```

It prints:

- current config
- active bridge module IDs
- default sink/source
- selected Bluetooth / ADB devices
- active call policy snapshot
- detected Bluetooth call-audio routes (`bluez_pairs`)
- BlueZ cards and active profiles
- paired/connected Bluetooth devices
- ADB devices
- current ADB call info
- active call session
- systemd user service status

Useful logs:

```bash
callscoot logs -f
journalctl --user -u callscoot-daemon.service -f
```

Useful low-level checks:

```bash
pactl list short cards
pactl list short sinks
pactl list short sources
bluetoothctl devices Paired
bluetoothctl devices Connected
```

---

## Building another app on top of CallScoot

The intended split is:

- **CallScoot** = runtime / telephony substrate / local API
- **your app** = UI / CRM logic / campaign logic / reporting

If you are building a Python app, you can vendor or import:

```text
src/callscoot_client.py
```

Minimal example:

```python
from callscoot_client import CallScootClient

client = CallScootClient(base_url="http://127.0.0.1:8788")
client.health()
client.queue_outbound_call(
    "+905551112233",
    dynamic_variables={"campaign_name": "survey"},
    metadata={"lead_id": "42"},
)
session_id = client.wait_for_session_start()
session = client.wait_for_session_end(session_id)
print(session["meta"].get("summary"))
```

For the full integration shape, see:

- [`docs/API.md`](docs/API.md)
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- [`examples/minimal_client_app.py`](examples/minimal_client_app.py)
- [`examples/lead_campaign_app.py`](examples/lead_campaign_app.py)

---

## Practical notes

- On Android, make sure the Bluetooth device is allowed for **call audio**
- During a call, Android may require selecting the Bluetooth device as the active audio route once
- Echo cancellation is enabled by default, but physical speaker volume still matters
- Headphones will always sound cleaner than laptop speakers, but speakerphone mode works
- If more than one Bluetooth audio device is around, set `--device` / `target_device`

---

## Security / privacy

- Audio stays local between your laptop and your phone
- No cloud relay
- No remote SaaS dependency
- ADB helpers are optional and local-only

---

## License

MIT
