# SIP telephony mode

CallScoot can now use a SIP account as its telephony backend.

That means outbound calls, incoming call state, session files, `current_call`, and the local API can run over SIP instead of Android ADB call control.

## Backend modes

CallScoot supports these backend values in config:

- `adb` - default Android + Bluetooth workflow
- `sip` - direct SIP registration and calling through `pjsua2`
- `auto` - use SIP when SIP is configured, otherwise fall back to ADB

SIP mode also has an audio sub-mode:

- `sip_audio_mode=direct` (default): SIP audio goes straight to local capture/playback device
- `sip_audio_mode=agent`: SIP audio streams are moved into `callscoot.agent.rx` / `callscoot.agent.tx.monitor` for AI agent integration

The active backend is visible in:

- `callscoot status`
- `GET /v1/status`

## Requirements

SIP mode depends on `pjsua2`.

Typical Linux prerequisites:

```bash
sudo apt-get install libpjproject-dev python3-dev
pip install pjsua2
```

`requirements.txt` includes `pjsua2`, but some distributions may still require manual system packages or a local build of pjproject bindings.

## Configure SIP from CLI

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

Optional device overrides (for `direct` mode):

```bash
callscoot configure \
  --sip-playback-device pipewire \
  --sip-capture-device pipewire
```

These are matched against audio devices exposed to `pjsua2`. If they are omitted, CallScoot falls back to `local_sink` and `local_source`, then finally to the system default devices.

Enable AI agent audio mode:

```bash
callscoot configure --sip-audio-mode agent
systemctl --user restart callscoot-agent.service
```

In `agent` mode CallScoot auto-routes SIP PipeWire streams to:

- playback stream -> `callscoot.agent.rx`
- capture stream <- `callscoot.agent.tx.monitor`

## Clear SIP config

```bash
callscoot configure --clear-sip
```

If the backend was `sip`, CallScoot switches back to `adb`.

## Using the local API

`callscoot dial`, `callscoot answer`, and `callscoot hangup` delegate SIP control to the long-running `callscoot-api` process, because the SIP endpoint must stay alive while the call is active.

When SIP is selected, `POST /v1/outbound-calls` uses SIP instead of ADB:

```bash
curl -X POST http://127.0.0.1:8788/v1/outbound-calls \
  -H 'Content-Type: application/json' \
  -d '{"number":"905551112233"}'
```

Example response:

```json
{
  "queued": true,
  "dialing": true,
  "via": "sip",
  "backend": "sip",
  "uri": "sip:905551112233@sip.example.com",
  "session_id": "20260411-120000-905551112233"
}
```

Other backend-aware API calls:

- `POST /v1/current-call/answer`
- `POST /v1/current-call/hangup`
- `GET /v1/status`
- `GET /v1/config`

## Session behavior

SIP calls create the same session files used by the rest of CallScoot:

- `~/.local/state/callscoot/current-call.json`
- `~/.local/state/callscoot/calls/<session_id>/meta.json`
- `~/.local/state/callscoot/calls/<session_id>/events.jsonl`

The session metadata includes:

- `telephony_backend: "sip"`
- direction
- normalized remote number
- current state

## Audio notes

SIP mode does **not** use the Bluetooth HFP bridge.

Instead, `pjsua2` opens local capture/playback devices directly. For AI usage, point SIP capture/playback at the same virtual devices that `callscoot-agent bootstrap-audio` creates.

Recommended pattern for AI mode:

1. bootstrap agent audio (once)
2. configure SIP in `agent` audio mode
3. keep `callscoot-agent.service` running
4. place calls through API/CLI

Example:

```bash
callscoot-agent bootstrap-audio
callscoot configure \
  --telephony-backend sip \
  --sip-server sip.example.com \
  --sip-username 1001 \
  --sip-password supersecret \
  --sip-capture-device pipewire \
  --sip-playback-device pipewire \
  --sip-audio-mode agent
systemctl --user restart callscoot-api.service callscoot-agent.service
```

## Security

CallScoot stores the SIP password in the local config file.

To reduce accidental exposure:

- `callscoot configure`
- `callscoot status`
- `GET /v1/config`
- `GET /v1/status`

mask `sip_password` as `***` in their output.
