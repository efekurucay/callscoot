# CallScoot

Turn a Linux laptop into a Bluetooth call deck for an Android phone.

CallScoot makes the laptop behave like a local call console:

- Android phone sends call audio to the laptop over **Bluetooth HFP/HSP**
- laptop microphone audio goes back to the phone
- laptop speakers play the remote caller
- optional **echo cancellation** is enabled by default
- optional **ADB helpers** can dial / answer / hang up calls
- runs as a **headless user service** on an always-on Linux box

This project is Linux-first and tested against:

- Debian 13 / BlueZ 5.82
- PipeWire 1.4.x
- WirePlumber 0.5.x
- Android phone over Bluetooth HFP/HSP

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

- Auto-detects Bluetooth HFP/HSP sink+source pairs
- Auto-switches BlueZ cards to an HFP profile when possible
- Echo cancellation via `module-echo-cancel`
- Two loopbacks via `module-loopback`
- Headless-friendly WirePlumber config (`seat-monitoring = disabled`)
- Systemd user service for always-on usage
- Optional ADB call helpers

---

## Repository layout

```text
bin/callscoot                 launcher
src/callscoot.py             main CLI + daemon
scripts/install-system.sh    root/system package setup
scripts/install-user.sh      user install + systemd service
systemd/callscoot-daemon.service
config/10-callscoot-bluetooth.conf
```

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

It also enables `bluetooth.service` and `loginctl enable-linger` for the target user.

### 2) User service

```bash
./scripts/install-user.sh
```

This installs:

- `~/.local/bin/callscoot`
- `~/.local/lib/callscoot/callscoot.py`
- `~/.config/systemd/user/callscoot-daemon.service`
- `~/.config/wireplumber/wireplumber.conf.d/10-callscoot-bluetooth.conf`

And it restarts PipeWire/WirePlumber and enables the daemon.

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
```

Clear pinned values:

```bash
callscoot configure --clear-device
callscoot configure --clear-adb-serial
```

---

## Optional ADB helpers

If your Android device is already reachable with ADB:

```bash
callscoot dial +905551112233
callscoot answer
callscoot hangup
```

These are convenience helpers only.
The actual audio path for calls should still be Bluetooth HFP/HSP.

---

## Status / debugging

```bash
callscoot status
```

It prints:

- current config
- active bridge module IDs
- default sink/source
- BlueZ sink+source pairs
- BlueZ cards and active profiles
- paired/connected Bluetooth devices
- ADB devices
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
