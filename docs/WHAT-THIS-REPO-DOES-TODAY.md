# What CallScoot does today

This document describes the repository **as it exists right now**.

## Short version

CallScoot is a **Linux-side Bluetooth call-audio bridge** for Android phones.

If Android routes call audio to the laptop over **Bluetooth HFP/HSP**, CallScoot connects:

- **phone -> laptop speakers**
- **laptop microphone -> phone**

using PipeWire / PipeWire Pulse modules.

## What problem it solves

Without CallScoot, pairing a phone to a Linux laptop is often not enough for a reliable always-on call setup on a headless machine.

CallScoot adds the missing Linux-side glue:

- Bluetooth pairing helpers
- WirePlumber configuration for Bluetooth call audio
- automatic HFP/HSP profile selection where possible
- automatic creation/removal of the PipeWire/Pulse bridge
- systemd user service for unattended operation
- optional ADB call control and call automation policies
- optional AI call-agent integration with transcript + summary logging

## Exact runtime behavior

When `callscoot-daemon.service` is running, it loops continuously and does the following:

1. checks PipeWire / PulseAudio-compatible state with `pactl`
2. looks for BlueZ cards and Bluetooth nodes created by the phone
3. searches for a matching Bluetooth call route, either as:
   - source (`bluez_input...`) + sink (`bluez_output...`) devices
   - or stream-based phone audio endpoints exposed by PipeWire
4. if available, tries to switch the BlueZ card to an HFP/HSP profile
5. creates these modules:
   - `module-echo-cancel` (default: enabled)
   - `module-loopback` for phone audio to laptop speakers
   - `module-loopback` for laptop mic audio to phone
6. stores module IDs in state so they can be removed cleanly later
7. tears the bridge down if the Bluetooth call route disappears

## Audio topology

### Default path

```text
Android phone call audio
        <-> Bluetooth HFP/HSP <->
Linux Bluetooth nodes
        -> loopback -> echo cancel sink -> laptop speakers
laptop microphone
        -> echo cancel source -> loopback -> phone Bluetooth sink
```

### In plain language

- remote caller is heard on the laptop
- your voice is captured from the laptop mic
- the phone remains the real telephony endpoint
- the laptop behaves like the live headset / speakerphone endpoint

## What is included in this repo

- `src/callscoot.py`
  - main CLI
  - daemon loop
  - Bluetooth helpers
  - PipeWire / Pulse bridge management
  - optional ADB call helpers
  - active-device selection
  - call policy engine
  - call-session logging
- `src/callscoot_agent.py`
  - optional STT -> LLM -> TTS pipeline
  - transcript writer
  - summary writer
- `scripts/install-system.sh`
  - installs system packages
  - enables Bluetooth
  - enables linger for headless user service use
- `scripts/install-user.sh`
  - installs the user CLI
  - installs the systemd user unit
  - installs the WirePlumber config
- `config/10-callscoot-bluetooth.conf`
  - enables Bluetooth roles needed for call audio
  - disables seat-monitoring restrictions for always-on use
- `systemd/callscoot-daemon.service`
  - keeps the bridge watcher alive in the background

## What is optional vs required

### Required for audio bridging

- Linux with BlueZ + PipeWire + WirePlumber
- phone paired over Bluetooth
- Android permission for **call audio** on the laptop Bluetooth device
- phone exposing a usable HFP/HSP route

### Optional

- ADB, only for:
  - `callscoot dial`
  - `callscoot answer`
  - `callscoot hangup`
  - auto-answer / auto-reject policy actions
  - call-state detection
- the optional `callscoot-agent` for STT / LLM / TTS

ADB is **not** used for call audio.

## What the repo is useful for, today

This repo is already useful if you want to:

- leave an old Linux laptop on at home
- pair your Android phone to it
- use the laptop as a desk speakerphone during calls
- auto-answer or auto-reject calls based on simple policies
- keep call logs and transcripts locally
- add an optional OpenAI / Ollama / whisper / espeak call agent later
- keep the setup running as a background service
- avoid proprietary desktop phone suites

## What it does not do

CallScoot currently does **not** include:

- Android app / companion service
- contact syncing
- natural language calling such as "call Ahmet"
- Web UI or desktop GUI
- direct telephony over custom Bluetooth AT command handling
- raw call-audio tunneling over ADB

## Current success criteria

A successful setup looks like this:

1. phone is paired and trusted
2. `callscoot status` shows an active Bluetooth call-audio route in `bluez_pairs`
3. the daemon creates loopback modules
4. during a phone call, sound is heard from the laptop speakers
5. your laptop mic is used for the conversation

## Recommended way to describe the repo

If you need a one-line description, use this:

> CallScoot turns a Linux laptop into a Bluetooth call deck for an Android phone by bridging HFP/HSP call audio between the phone and the laptop mic/speakers.
