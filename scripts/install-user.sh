#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$HOME/.local/lib/callscoot"
VENV_DIR="$INSTALL_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
WIREPLUMBER_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$SERVICE_DIR" "$WIREPLUMBER_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
if [[ -f "$ROOT_DIR/requirements.txt" ]]; then
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt" >/dev/null
fi
cp "$ROOT_DIR/src/callscoot.py" "$INSTALL_DIR/callscoot.py"
cp "$ROOT_DIR/src/callscoot_agent.py" "$INSTALL_DIR/callscoot_agent.py"
cp "$ROOT_DIR/src/elevenlabs_agent.py" "$INSTALL_DIR/elevenlabs_agent.py"
cp "$ROOT_DIR/src/agent_orchestrator.py" "$INSTALL_DIR/agent_orchestrator.py"
cp "$ROOT_DIR/src/audio_bridge.py" "$INSTALL_DIR/audio_bridge.py"
cp "$ROOT_DIR/src/agent_tools.py" "$INSTALL_DIR/agent_tools.py"
cp "$ROOT_DIR/src/agent_memory.py" "$INSTALL_DIR/agent_memory.py"
cp "$ROOT_DIR/src/agent_state.py" "$INSTALL_DIR/agent_state.py"
cp "$ROOT_DIR/src/agent_events.py" "$INSTALL_DIR/agent_events.py"
cp "$ROOT_DIR/src/webhook_server.py" "$INSTALL_DIR/webhook_server.py"
chmod +x "$INSTALL_DIR/callscoot.py" "$INSTALL_DIR/callscoot_agent.py" "$INSTALL_DIR/elevenlabs_agent.py" "$INSTALL_DIR/agent_orchestrator.py" "$INSTALL_DIR/webhook_server.py"
cat > "$BIN_DIR/callscoot" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PYTHON_BIN="$HOME/.local/lib/callscoot/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
exec "$PYTHON_BIN" "$HOME/.local/lib/callscoot/callscoot.py" "$@"
EOF
cat > "$BIN_DIR/callscoot-agent" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PYTHON_BIN="$HOME/.local/lib/callscoot/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
exec "$PYTHON_BIN" "$HOME/.local/lib/callscoot/callscoot_agent.py" "$@"
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
