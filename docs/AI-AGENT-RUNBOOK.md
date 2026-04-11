# AI agent runbook

The default production path is now the **ElevenAgents runtime**.

CallScoot runs as two local services:

- `callscoot-agent.service` -> real-time agent runtime
- `callscoot-api.service` -> local control API for external apps

## Runtime responsibilities

The agent runtime is responsible for:

- reading call audio from `callscoot.agent.rx.monitor`
- sending audio to ElevenAgents over WebSocket
- receiving generated agent audio and playing it into `callscoot.agent.tx`
- persisting transcripts, summaries, and structured events per call
- reconnecting the audio / websocket path when possible

## Services

```bash
systemctl --user status callscoot-daemon.service
systemctl --user status callscoot-agent.service
systemctl --user status callscoot-api.service
```

## Environment

Agent runtime configuration is loaded from:

```text
~/.config/callscoot/elevenagents.env
```

Expected values:

```env
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
# optional:
# ELEVENLABS_WEBHOOK_SECRET=...
# CALLSCOOT_API_HOST=127.0.0.1
# CALLSCOOT_API_PORT=8788
# CALLSCOOT_API_TOKEN=
```

## Audio devices

CallScoot uses these virtual devices for the agent path:

- `callscoot.agent.rx`
- `callscoot.agent.rx.monitor`
- `callscoot.agent.tx`
- `callscoot.agent.tx.monitor`

## What to check first

### 1) Services

```bash
systemctl --user status callscoot-daemon.service
systemctl --user status callscoot-agent.service
systemctl --user status callscoot-api.service
```

### 2) Logs

```bash
journalctl --user -u callscoot-agent.service -f
journalctl --user -u callscoot-daemon.service -f
journalctl --user -u callscoot-api.service -f
```

### 3) API health

```bash
curl http://127.0.0.1:8788/v1/health
curl http://127.0.0.1:8788/v1/status
```

## Outbound call test

Queue a call with injected context:

```bash
curl -X POST http://127.0.0.1:8788/v1/outbound-calls \
  -H 'Content-Type: application/json' \
  -d '{
    "number": "+905551112233",
    "dynamic_variables": {
      "campaign_name": "smoke_test",
      "contact_name": "Efe"
    },
    "metadata": {
      "row_id": "1"
    }
  }'
```

Then watch live agent events:

```bash
curl -N 'http://127.0.0.1:8788/v1/events/stream?session_id=current'
```

## After the call

Session artifacts are stored under:

```text
~/.local/state/callscoot/calls/<session-id>/
```

Typical files:

- `meta.json`
- `events.jsonl`
- `transcript.jsonl`
- `agent_events.jsonl`
- `summary.txt`

## External app usage

If another app needs to drive CallScoot, it should use the local API rather than shelling out to Bluetooth or PipeWire tools.

See [`API.md`](API.md) and [`../examples/lead_campaign_app.py`](../examples/lead_campaign_app.py).

## Legacy mode

`callscoot-agent run --mode classic` still exists for the older local STT -> LLM -> TTS pipeline.

The production recommendation is:

```bash
callscoot-agent run --mode elevenagents
```
