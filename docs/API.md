# CallScoot Local API

CallScoot exposes a local HTTP API so another app can control calls without embedding telephony logic.

Default bind:

- host: `127.0.0.1`
- port: `8788`

Service:

```bash
systemctl --user status callscoot-api.service
```

## What the API is for

A separate app can:

- queue an outbound call
- attach dynamic variables / campaign context to the next call
- inspect and patch runtime configuration
- inspect current and past sessions
- stream structured agent events
- inject contextual updates into the active call or a specific session
- hang up the current call
- manage pending outbound call requests

CallScoot stays responsible for:

- Bluetooth call routing
- ElevenAgents session lifecycle
- transcripts and summaries
- stable session IDs and event files

Your app stays responsible for:

- lead lists
- CRM / Sheets / Airtable writes
- campaign logic
- retry policy
- business workflows

## Endpoints

### Health

```bash
curl http://127.0.0.1:8788/v1/health
```

### Status

```bash
curl http://127.0.0.1:8788/v1/status
```

### Read config

```bash
curl http://127.0.0.1:8788/v1/config
```

### Patch config

```bash
curl -X PATCH http://127.0.0.1:8788/v1/config \
  -H 'Content-Type: application/json' \
  -d '{
    "auto_answer": true,
    "auto_answer_delay_sec": 2,
    "max_call_duration_sec": 600,
    "echo_cancel": false
  }'
```

### Queue an outbound call

```bash
curl -X POST http://127.0.0.1:8788/v1/outbound-calls \
  -H 'Content-Type: application/json' \
  -d '{
    "number": "+905551112233",
    "dynamic_variables": {
      "campaign_name": "lead_qualification",
      "contact_name": "Efe",
      "company_name": "Acme"
    },
    "metadata": {
      "lead_id": "lead-123",
      "list_id": "batch-2026-04-11"
    },
    "ttl_sec": 300
  }'
```

This does two things:

1. stores a pending request for the next matching call session
2. starts the Android phone call over ADB

When the live session starts, the pending request is claimed and injected into the agent as dynamic variables.

### Queue call context without dialing yet

```bash
curl -X POST http://127.0.0.1:8788/v1/pending-call-requests \
  -H 'Content-Type: application/json' \
  -d '{
    "target_number": "+905551112233",
    "dynamic_variables": {
      "campaign_name": "renewal_reminder"
    },
    "metadata": {
      "customer_id": "cust-88"
    }
  }'
```

### Get one pending request

```bash
curl http://127.0.0.1:8788/v1/pending-call-requests/REQUEST_ID
```

### Delete one pending request

```bash
curl -X DELETE http://127.0.0.1:8788/v1/pending-call-requests/REQUEST_ID
```

### Current call

```bash
curl http://127.0.0.1:8788/v1/current-call
```

### List recent calls

```bash
curl 'http://127.0.0.1:8788/v1/calls?limit=20'
```

### Get one session

```bash
curl http://127.0.0.1:8788/v1/calls/SESSION_ID
```

### Get structured agent events for one session

```bash
curl http://127.0.0.1:8788/v1/calls/SESSION_ID/events
```

### Stream events over SSE

```bash
curl -N 'http://127.0.0.1:8788/v1/events/stream?session_id=current'
```

Event stream includes items such as:

- `call_started`
- `conversation_started`
- `transcript_final`
- `agent_response_started`
- `agent_response_finished`
- `interruption`
- `client_error`
- `call_ended`

### Inject a contextual update into the active call

```bash
curl -X POST http://127.0.0.1:8788/v1/current-call/contextual-update \
  -H 'Content-Type: application/json' \
  -d '{"text":"The CRM says this caller is a warm lead. Keep the conversation under one minute."}'
```

This is non-interrupting guidance for the running agent.

### Inject a contextual update into a specific session

```bash
curl -X POST http://127.0.0.1:8788/v1/calls/SESSION_ID/contextual-update \
  -H 'Content-Type: application/json' \
  -d '{"text":"You already confirmed the callback time. Wrap up the call politely."}'
```

### Send a user-style message into the active session

```bash
curl -X POST http://127.0.0.1:8788/v1/current-call/user-message \
  -H 'Content-Type: application/json' \
  -d '{"text":"Ask for a convenient callback time before ending the call."}'
```

### Send a user-style message into a specific session

```bash
curl -X POST http://127.0.0.1:8788/v1/calls/SESSION_ID/user-message \
  -H 'Content-Type: application/json' \
  -d '{"text":"Ask for an email address before ending."}'
```

### Hang up current call

```bash
curl -X POST http://127.0.0.1:8788/v1/current-call/hangup
```

## Example integration from another Python app

A zero-dependency Python helper is included at:

```text
src/callscoot_client.py
```

A complete sequential lead-calling example is included here:

```text
examples/lead_campaign_app.py
examples/leads.csv.example
examples/minimal_client_app.py
```

It reads a CSV lead list, queues calls one by one, waits for completion, and writes results back into the same CSV.

### Minimal Python snippet

```python
from callscoot_client import CallScootClient

client = CallScootClient(base_url="http://127.0.0.1:8788")
client.health()
client.queue_outbound_call(
    "+905551112233",
    dynamic_variables={
        "campaign_name": "survey",
        "contact_name": "Efe",
    },
    metadata={
        "row_id": "42",
    },
)
session_id = client.wait_for_session_start()
session = client.wait_for_session_end(session_id)
print(session["meta"].get("summary"))
```

## Authentication

If you want a bearer token on the local API, set:

```env
CALLSCOOT_API_TOKEN=your_token_here
```

Then call with:

```bash
-H 'Authorization: Bearer your_token_here'
```

## Environment

Optional API environment variables:

```env
CALLSCOOT_API_HOST=127.0.0.1
CALLSCOOT_API_PORT=8788
CALLSCOOT_API_TOKEN=
```

If your external client app uses `src/callscoot_client.py`, it can also use:

```env
CALLSCOOT_API_BASE=http://127.0.0.1:8788
```

The SSE event stream sends heartbeat comments regularly so long-lived consumers can keep the connection open more reliably.
