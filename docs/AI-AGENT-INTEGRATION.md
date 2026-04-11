# AI agent integration

CallScoot should be treated as a **local call runtime**.

Do not make your app talk to PipeWire, BlueZ, or Bluetooth directly.
Use the local CallScoot API instead.

See:

- [`API.md`](API.md)
- [`AI-AGENT-RUNBOOK.md`](AI-AGENT-RUNBOOK.md)
- [`AI-VOICE-ROUTING.md`](AI-VOICE-ROUTING.md)

## Recommended integration shape

Your application is responsible for:

- lead lists
- campaign logic
- CRM writes
- retries
- business rules

CallScoot is responsible for:

- Bluetooth call audio routing
- ElevenAgents session lifecycle
- transcripts, summaries, and structured event files
- local call/session control

## Preferred control path

Use the local HTTP API:

```text
POST /v1/outbound-calls
GET  /v1/current-call
GET  /v1/calls
GET  /v1/calls/<session-id>
GET  /v1/events/stream?session_id=current
POST /v1/current-call/contextual-update
POST /v1/current-call/user-message
POST /v1/current-call/hangup
```

## Typical architecture

```text
Your app
   -> CallScoot local HTTP API
   -> CallScoot agent runtime
   -> ElevenAgents
   -> Bluetooth phone call
```

## Example use cases

- lead qualification campaign
- appointment confirmation
- renewal reminder calls
- customer survey calls
- outbound concierge assistant

## Minimal control loop

1. queue a call with `POST /v1/outbound-calls`
2. inject campaign data through `dynamic_variables`
3. watch `GET /v1/events/stream?session_id=current`
4. fetch final call data from `GET /v1/calls/<session-id>`
5. write results to your own database / CRM / spreadsheet

## Example app

A minimal sequential lead-calling app is included here:

```text
examples/lead_campaign_app.py
```

That script reads a CSV file, places calls one by one, and writes results back into the file.
