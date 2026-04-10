# AI voice routing

This document explains the exact setup for this workflow:

1. phone call audio comes into the computer
2. your software processes that audio locally
3. your software generates a reply
4. the generated reply is sent back into the phone call

In other words, this is the document for using CallScoot as the audio bridge for an **STT -> LLM -> TTS -> phone call** pipeline.

---

## Short version

Use **two virtual Pulse/PipeWire sinks**:

- one sink for **incoming call audio** from the phone to your AI pipeline
- one sink for **outgoing AI speech** from your pipeline back to the phone

Then point CallScoot at them like this:

```bash
callscoot configure \
  --sink callscoot.agent.rx \
  --source callscoot.agent.tx.monitor \
  --echo-cancel off
```

Meaning:

- phone audio is delivered into sink `callscoot.agent.rx`
- your agent reads that audio from `callscoot.agent.rx.monitor`
- your agent writes generated audio into sink `callscoot.agent.tx`
- CallScoot reads that generated audio from `callscoot.agent.tx.monitor`
- CallScoot sends it back to the phone call over Bluetooth HFP/HSP

That is the whole idea.

---

## Topology

```text
remote caller
   -> Android phone call
   -> Bluetooth HFP/HSP
   -> CallScoot
   -> sink: callscoot.agent.rx
   -> monitor source: callscoot.agent.rx.monitor
   -> STT / LLM / TTS pipeline
   -> sink: callscoot.agent.tx
   -> monitor source: callscoot.agent.tx.monitor
   -> CallScoot
   -> Bluetooth HFP/HSP
   -> Android phone call
   -> remote caller
```

---

## Important rule

For a full AI audio pipeline, set:

```bash
callscoot configure --echo-cancel off
```

Reason:

- echo cancellation is useful for real laptop mic + speaker use
- for a fully virtual AI pipeline, it usually adds unnecessary processing
- your agent should receive the rawest practical call audio path

---

## Prerequisites

Before doing anything below:

1. CallScoot must already be installed
2. the phone must be paired with the laptop
3. Android must allow the laptop Bluetooth device for **call audio**
4. `callscoot status` should eventually show an active Bluetooth call-audio route in `bluez_pairs` when the phone route is active

---

## Step 1 — create the virtual audio devices

Create two virtual sinks manually:

```bash
pactl load-module module-null-sink \
  sink_name=callscoot.agent.rx \
  sink_properties=device.description=CallScoot-AI-RX \
  rate=16000 channels=1 channel_map=mono

pactl load-module module-null-sink \
  sink_name=callscoot.agent.tx \
  sink_properties=device.description=CallScoot-AI-TX \
  rate=16000 channels=1 channel_map=mono
```

Or let the bundled helper do it for you:

```bash
callscoot-agent bootstrap-audio
```

This creates:

- sink `callscoot.agent.rx`
- monitor source `callscoot.agent.rx.monitor`
- sink `callscoot.agent.tx`
- monitor source `callscoot.agent.tx.monitor`

Check them:

```bash
pactl list short sinks | grep 'callscoot.agent'
pactl list short sources | grep 'callscoot.agent'
```

Expected shape:

```text
callscoot.agent.rx
callscoot.agent.rx.monitor
callscoot.agent.tx
callscoot.agent.tx.monitor
```

---

## Step 2 — tell CallScoot to use those devices

Configure CallScoot like this:

```bash
callscoot configure \
  --sink callscoot.agent.rx \
  --source callscoot.agent.tx.monitor \
  --echo-cancel off
```

Meaning:

- `--sink callscoot.agent.rx`
  - incoming phone audio will be sent into this virtual sink
- `--source callscoot.agent.tx.monitor`
  - outgoing audio to the phone will be read from this monitor source
- `--echo-cancel off`
  - disables AEC for the virtual AI path

If `callscoot-daemon.service` is already running, restart it so the new config is picked up:

```bash
systemctl --user restart callscoot-daemon.service
```

You can also force it immediately:

```bash
callscoot down
callscoot up
```

---

## Step 3 — verify the bridge

Run:

```bash
callscoot status
```

What you want to see:

- `service_active` = `active`
- `bluez_pairs` contains the phone's active Bluetooth call-audio route
- your config contains:
  - `local_sink = callscoot.agent.rx`
  - `local_source = callscoot.agent.tx.monitor`
  - `echo_cancel = false`

When the phone route is active, CallScoot will create the bridge automatically.

---

## Step 4 — receive the phone audio inside the computer

Your AI pipeline should record from:

```text
callscoot.agent.rx.monitor
```

### Raw PCM example

```bash
pacat --record \
  --device=callscoot.agent.rx.monitor \
  --rate=16000 \
  --channels=1 \
  --format=s16le \
  --raw
```

This writes mono 16 kHz 16-bit PCM to stdout.
That stdout can go into your STT pipeline.

### Save 10 seconds to a file

```bash
timeout 10 pacat --record \
  --device=callscoot.agent.rx.monitor \
  --rate=16000 \
  --channels=1 \
  --format=s16le \
  --raw > incoming-call.raw
```

---

## Step 5 — send generated speech back into the phone call

Your AI pipeline should play audio into:

```text
callscoot.agent.tx
```

### Raw PCM example

If your TTS produces mono 16 kHz 16-bit PCM:

```bash
cat reply.raw | pacat --playback \
  --device=callscoot.agent.tx \
  --rate=16000 \
  --channels=1 \
  --format=s16le \
  --raw
```

### WAV example

If your TTS produces a WAV file:

```bash
paplay --device=callscoot.agent.tx reply.wav
```

CallScoot will read that audio from `callscoot.agent.tx.monitor` and forward it into the live phone call.

---

## The two names you actually need to remember

### Incoming call audio for the agent to read

```text
callscoot.agent.rx.monitor
```

### Outgoing generated audio for the agent to write into

```text
callscoot.agent.tx
```

That is the key pair.

---

## Minimal end-to-end shell test

### 1) Create the virtual devices

```bash
pactl load-module module-null-sink sink_name=callscoot.agent.rx sink_properties=device.description=CallScoot-AI-RX rate=16000 channels=1 channel_map=mono
pactl load-module module-null-sink sink_name=callscoot.agent.tx sink_properties=device.description=CallScoot-AI-TX rate=16000 channels=1 channel_map=mono
```

### 2) Point CallScoot at them

```bash
callscoot configure --sink callscoot.agent.rx --source callscoot.agent.tx.monitor --echo-cancel off
systemctl --user restart callscoot-daemon.service
```

### 3) Start / force the bridge

```bash
callscoot up
```

### 4) During a live call, capture incoming phone audio

```bash
pacat --record --device=callscoot.agent.rx.monitor --rate=16000 --channels=1 --format=s16le --raw > incoming.raw
```

### 5) Send a generated reply back into the call

```bash
cat reply.raw | pacat --playback --device=callscoot.agent.tx --rate=16000 --channels=1 --format=s16le --raw
```

If step 5 runs while the call is active, the far side should hear that generated audio.

---

## Minimal Python pattern

This is the simplest process model for an agent:

```python
import subprocess

rx = subprocess.Popen(
    [
        "pacat",
        "--record",
        "--device=callscoot.agent.rx.monitor",
        "--rate=16000",
        "--channels=1",
        "--format=s16le",
        "--raw",
    ],
    stdout=subprocess.PIPE,
)

tx = subprocess.Popen(
    [
        "pacat",
        "--playback",
        "--device=callscoot.agent.tx",
        "--rate=16000",
        "--channels=1",
        "--format=s16le",
        "--raw",
    ],
    stdin=subprocess.PIPE,
)

# rx.stdout: incoming call PCM for STT
# tx.stdin: write generated PCM for playback into the call
```

Typical loop:

1. read PCM chunks from `rx.stdout`
2. run VAD / STT
3. send text to LLM
4. synthesize reply with TTS into 16 kHz mono PCM
5. write that PCM to `tx.stdin`

That is enough to build a phone-call AI responder.

---

## Bootstrap script example

If you want your agent stack to create the virtual devices automatically, use a small bootstrap step like this:

```bash
#!/usr/bin/env bash
set -euo pipefail

if ! pactl list short sinks | awk '{print $2}' | grep -qx 'callscoot.agent.rx'; then
  pactl load-module module-null-sink \
    sink_name=callscoot.agent.rx \
    sink_properties=device.description=CallScoot-AI-RX \
    rate=16000 channels=1 channel_map=mono >/dev/null
fi

if ! pactl list short sinks | awk '{print $2}' | grep -qx 'callscoot.agent.tx'; then
  pactl load-module module-null-sink \
    sink_name=callscoot.agent.tx \
    sink_properties=device.description=CallScoot-AI-TX \
    rate=16000 channels=1 channel_map=mono >/dev/null
fi

callscoot configure \
  --sink callscoot.agent.rx \
  --source callscoot.agent.tx.monitor \
  --echo-cancel off >/dev/null

systemctl --user restart callscoot-daemon.service
```

Run that before starting your AI process.

---

## Common mistakes

### 1) Reading from the wrong device

Read from:

```text
callscoot.agent.rx.monitor
```

not from `callscoot.agent.rx` itself.

### 2) Writing to the wrong device

Write generated audio to:

```text
callscoot.agent.tx
```

not to `callscoot.agent.tx.monitor`.

### 3) Leaving echo cancellation on

For virtual-only AI routing, set:

```bash
callscoot configure --echo-cancel off
```

### 4) Forgetting to restart the daemon after `configure`

If the daemon is already running:

```bash
systemctl --user restart callscoot-daemon.service
```

### 5) No active Bluetooth call route

If the phone is not exposing an HFP/HSP route, `callscoot up` cannot build the live bridge.
Check:

```bash
callscoot status
```

and look at `bluez_pairs`.

---

## What your AI system should own vs what CallScoot should own

### CallScoot owns

- Bluetooth call-audio bridging
- BlueZ / PipeWire route selection
- loopbacks between phone route and chosen local endpoints
- optional ADB call-control helpers

### Your AI system owns

- VAD
- STT
- LLM inference
- TTS
- conversation state
- deciding when to speak
- writing generated audio to `callscoot.agent.tx`
- reading incoming audio from `callscoot.agent.rx.monitor`

That separation is the intended design.

---

## One-sentence summary

To build an AI phone-call responder on top of CallScoot, route the call into `callscoot.agent.rx.monitor`, generate speech, write it to `callscoot.agent.tx`, and let CallScoot bridge that audio to and from the Android phone over Bluetooth HFP/HSP.
