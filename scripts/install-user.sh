#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$HOME/.local/lib/callscoot"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
WIREPLUMBER_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$SERVICE_DIR" "$WIREPLUMBER_DIR"
cp "$ROOT_DIR/src/callscoot.py" "$INSTALL_DIR/callscoot.py"
chmod +x "$INSTALL_DIR/callscoot.py"
cat > "$BIN_DIR/callscoot" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$HOME/.local/lib/callscoot/callscoot.py" "$@"
EOF
chmod +x "$BIN_DIR/callscoot"
cp "$ROOT_DIR/systemd/callscoot-daemon.service" "$SERVICE_DIR/callscoot-daemon.service"
cp "$ROOT_DIR/config/10-callscoot-bluetooth.conf" "$WIREPLUMBER_DIR/10-callscoot-bluetooth.conf"

"$BIN_DIR/callscoot" install-user-config >/dev/null
systemctl --user daemon-reload
systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
systemctl --user enable --now callscoot-daemon.service

echo "[callscoot] user install complete"
echo "[callscoot] binary: $BIN_DIR/callscoot"
echo "[callscoot] service: systemctl --user status callscoot-daemon.service"
