#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install-system.sh [username]" >&2
  exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-}}"
if [[ -z "$TARGET_USER" ]]; then
  echo "Target user missing. Usage: sudo ./scripts/install-system.sh <username>" >&2
  exit 1
fi

apt-get update
apt-get install -y \
  bluez \
  pipewire \
  pipewire-bin \
  pipewire-pulse \
  pipewire-alsa \
  wireplumber \
  libspa-0.2-bluetooth \
  pulseaudio-utils \
  adb \
  jq \
  espeak-ng

systemctl enable --now bluetooth.service
loginctl enable-linger "$TARGET_USER"

echo "[callscoot] system packages installed"
echo "[callscoot] linger enabled for $TARGET_USER"
