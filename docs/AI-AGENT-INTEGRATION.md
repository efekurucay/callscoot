# AI agent integration

This is the shortest practical way to connect an AI agent to CallScoot.

## Mental model

Treat CallScoot as a **CLI backend**.

Your agent should not talk to PipeWire or BlueZ directly for control.
Instead, the agent should run `callscoot` commands and read the output.

For the **actual audio stream** case — receiving live call audio in the computer, running STT/LLM/TTS, and sending generated speech back into the call — see:

- [`AI-VOICE-ROUTING.md`](AI-VOICE-ROUTING.md)

## Recommended integration shape

Expose these commands as agent tools:

| Tool name | Command |
|---|---|
| `callscoot_status` | `callscoot status` |
| `callscoot_up` | `callscoot up` |
| `callscoot_down` | `callscoot down` |
| `callscoot_dial` | `callscoot dial <NUMBER>` |
| `callscoot_answer` | `callscoot answer` |
| `callscoot_hangup` | `callscoot hangup` |
| `callscoot_logs` | `callscoot logs -n 100` |

If you want device-specific control, also expose:

| Tool name | Command |
|---|---|
| `callscoot_devices` | `callscoot devices` |
| `callscoot_configure_device` | `callscoot configure --device <MAC>` |

## One-time human setup

Before an agent can use CallScoot reliably, do this once:

1. install CallScoot
2. pair the Android phone with `callscoot pair`
3. allow **call audio** for the laptop on Android
4. optionally pin the phone:

```bash
callscoot configure --device AA:BB:CC:DD:EE:FF
```

After that, the agent normally only needs `status`, `up/down`, and optional ADB helpers.

## What the agent should do at runtime

### To check whether the system is ready

Run:

```bash
callscoot status
```

Useful fields in the JSON output:

- `service_active`
- `bluez_pairs`
- `bluez_cards`
- `default_sink`
- `default_source`
- `adb_devices`
- `adb_call_state`

### To force the bridge now

```bash
callscoot up
```

### To remove the bridge

```bash
callscoot down
```

### To place or control a call

```bash
callscoot dial +905551112233
callscoot answer
callscoot hangup
```

Note: `dial/answer/hangup` use ADB if available. Call audio still uses Bluetooth HFP/HSP. If you want the daemon to answer ringing calls automatically, configure `callscoot configure --auto-answer on`.

## Minimal agent policy

A simple policy is enough:

- use `callscoot status` first
- if no Bluetooth call-audio route is present in `bluez_pairs`, do not pretend the bridge is active
- use `callscoot up` only when a phone route exists
- use `callscoot dial` only when the user explicitly asked to call a number
- use `callscoot logs` when diagnosis is needed

## Example wrapper

If your agent framework supports shell tools, a minimal wrapper is enough:

```bash
callscoot status
callscoot up
callscoot down
callscoot dial +905551112233
callscoot answer
callscoot hangup
```

## Recommended architecture

```text
AI agent
   -> shell/tool call
   -> callscoot CLI
   -> BlueZ / PipeWire / ADB
```

That is the intended integration model.

## Do not overcomplicate it

For a single-phone setup, the best approach is:

- keep `callscoot-daemon.service` running
- let the agent call the CLI only when needed
- use `status` as the source of truth

If you need one sentence:

> To integrate an AI agent with CallScoot, expose the `callscoot` CLI as agent tools and let the agent control the phone/audio bridge only through those commands.
