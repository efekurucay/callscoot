#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$HOME/.local/lib/callscoot"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
WIREPLUMBER_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$SERVICE_DIR" "$WIREPLUMBER_DIR"
cp "$ROOT_DIR/src/callscoot.py" "$INSTALL_DIR/callscoot.py"
cp "$ROOT_DIR/src/callscoot_agent.py" "$INSTALL_DIR/callscoot_agent.py"
chmod +x "$INSTALL_DIR/callscoot.py" "$INSTALL_DIR/callscoot_agent.py"
cat > "$BIN_DIR/callscoot" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$HOME/.local/lib/callscoot/callscoot.py" "$@"
EOF
cat > "$BIN_DIR/callscoot-agent" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$HOME/.local/lib/callscoot/callscoot_agent.py" "$@"
EOF
chmod +x "$BIN_DIR/callscoot" "$BIN_DIR/callscoot-agent"
cp "$ROOT_DIR/systemd/callscoot-daemon.service" "$SERVICE_DIR/callscoot-daemon.service"
cp "$ROOT_DIR/systemd/callscoot-agent.service" "$SERVICE_DIR/callscoot-agent.service"
cp "$ROOT_DIR/config/10-callscoot-bluetooth.conf" "$WIREPLUMBER_DIR/10-callscoot-bluetooth.conf"

"$BIN_DIR/callscoot" install-user-config >/dev/null
systemctl --user daemon-reload
systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
systemctl --user enable --now callscoot-daemon.service

echo "[callscoot] user install complete"
echo "[callscoot] binary: $BIN_DIR/callscoot"
echo "[callscoot] ai binary: $BIN_DIR/callscoot-agent"
echo "[callscoot] daemon: systemctl --user status callscoot-daemon.service"
echo "[callscoot] ai agent (optional): systemctl --user status callscoot-agent.service"
