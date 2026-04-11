# CallScoot deployment and integration model

CallScoot is best treated as a **self-hosted call runtime**.

It is not primarily an embedded telephony library.
It is the process that stays close to the phone, Bluetooth stack, PipeWire graph, and ElevenAgents session.

## Recommended product shape

```text
client app
   -> CallScoot local API
   -> CallScoot runtime services
   -> ElevenAgents
   -> Android phone over Bluetooth HFP/HSP
```

## What runs where

### CallScoot runtime host

This is the Linux machine that is physically paired to the Android phone.

It runs:

- `callscoot-daemon.service`
- `callscoot-agent.service`
- `callscoot-api.service`
- BlueZ / PipeWire / WirePlumber
- optional ADB access to the phone

This machine must stay close to the phone because:

- Bluetooth call audio lives here
- PipeWire routing lives here
- ADB helpers live here
- the real-time agent session is tied to the active call here

### Client app

This is your workflow application.

It can run:

- on the same Linux machine
- on another machine on the same network
- on a server behind a VPN / tunnel

It is responsible for:

- lead selection
- campaign logic
- CRM writes
- retries
- reporting
- dashboards / UI / operator workflow

## Best deployment options

### Option A: same machine

This is the easiest and recommended default.

```text
Linux box
  - CallScoot services
  - your client app
```

Use `http://127.0.0.1:8788`.

Advantages:

- simplest setup
- no network exposure needed
- lowest operational friction
- easiest auth story

### Option B: separate client app host

This is also valid if you want a remote dashboard or orchestrator.

```text
machine A: CallScoot runtime + phone
machine B: client app
```

In that case you should add your own network and security layer:

- reverse proxy or tunnel
- bearer token
- firewall rules
- preferably VPN / Tailscale / WireGuard

## How another developer should use CallScoot

### 1) Install the runtime on the phone-adjacent Linux box

```bash
git clone https://github.com/efekurucay/callscoot.git
cd callscoot
sudo ./scripts/install-system.sh "$USER"
./scripts/install-user.sh
```

### 2) Configure ElevenAgents

```bash
cp config/elevenagents.env.example ~/.config/callscoot/elevenagents.env
nano ~/.config/callscoot/elevenagents.env
```

### 3) Pair the phone and verify API health

```bash
callscoot pair
callscoot devices
curl http://127.0.0.1:8788/v1/health
```

### 4) Build the client app on top of the API

The client app should use:

- `docs/API.md`
- `src/callscoot_client.py`
- `examples/minimal_client_app.py`
- `examples/lead_campaign_app.py`

## Python client helper

A zero-dependency Python helper is included at:

```text
src/callscoot_client.py
```

Another Python app can:

- vendor that file directly into its own repo
- import it from a checkout of this repo
- copy it as the starting point for its own SDK

Example:

```python
from callscoot_client import CallScootClient

client = CallScootClient(base_url="http://127.0.0.1:8788")
client.health()
resp = client.queue_outbound_call(
    "+905551112233",
    dynamic_variables={"campaign_name": "survey"},
    metadata={"lead_id": "42"},
)
session_id = client.wait_for_session_start()
session = client.wait_for_session_end(session_id)
print(session["meta"].get("summary"))
```

## Why there is no separate TUI/GUI in this repo

If you are building a real client app, that app is already the user-facing interface.

So CallScoot stays focused on:

- telephony runtime
- local API
- session lifecycle
- transcripts and events

And your client app owns:

- the UI
- business settings
- workflow screens
- result handling

## Final recommendation

Treat CallScoot as:

> a self-hosted call runtime with a local API

Treat your own application as:

> the product, UI, and business workflow layer
