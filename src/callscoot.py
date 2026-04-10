#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import pty
import fnmatch
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP = "callscoot"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = STATE_DIR / "bridge-state.json"
CURRENT_CALL_PATH = STATE_DIR / "current-call.json"
CALLS_DIR = STATE_DIR / "calls"
WIREPLUMBER_CONFIG_PATH = Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d" / "10-callscoot-bluetooth.conf"
SERVICE_NAME = "callscoot-daemon.service"
ECHO_SOURCE_NAME = "callscoot.echo.src"
ECHO_SINK_NAME = "callscoot.echo.sink"
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULTS: dict[str, Any] = {
    "target_device": None,
    "local_sink": None,
    "local_source": None,
    "echo_cancel": True,
    "latency_msec": 60,
    "adb_serial": None,
    "discoverable_timeout": 180,
    "auto_answer": False,
    "auto_answer_delay_sec": 2,
    "auto_select_device": True,
    "device_bindings": {},
    "call_policy_mode": "allow_all",
    "allowed_callers": [],
    "blocked_callers": [],
    "unknown_callers": "allow",
    "business_hours": None,
    "business_days": WEEKDAY_NAMES[:],
    "auto_reject_blocked": False,
    "log_calls": True,
}
LAST_FORCE_HFP: dict[str, float] = {}
STOP = False


def log(message: str) -> None:
    print(f"[{APP}] {message}", flush=True)


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CALLS_DIR.mkdir(parents=True, exist_ok=True)


class CommandError(RuntimeError):
    pass


def run(cmd: list[str], check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, check=False, text=text, capture_output=True)
    except FileNotFoundError as exc:
        raise CommandError(f"command not found: {cmd[0]}") from exc
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise CommandError(f"{' '.join(cmd)} -> {detail}")
    return result


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise CommandError(f"required binary is missing: {name}")


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_config() -> dict[str, Any]:
    ensure_dirs()
    cfg = DEFAULTS.copy()
    cfg.update(load_json_file(CONFIG_PATH, {}))
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    ensure_dirs()
    save_json_file(CONFIG_PATH, cfg)


def load_state() -> dict[str, Any]:
    ensure_dirs()
    return load_json_file(STATE_PATH, {})


def save_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    save_json_file(STATE_PATH, state)


def clear_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


def load_current_call() -> dict[str, Any] | None:
    ensure_dirs()
    return load_json_file(CURRENT_CALL_PATH, None)


def save_current_call(data: dict[str, Any]) -> None:
    ensure_dirs()
    save_json_file(CURRENT_CALL_PATH, data)


def clear_current_call() -> None:
    if CURRENT_CALL_PATH.exists():
        CURRENT_CALL_PATH.unlink()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def call_session_dir(session_id: str) -> Path:
    ensure_dirs()
    return CALLS_DIR / session_id


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def normalize_phone_number(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("+"):
        return "+" + re.sub(r"\D", "", cleaned)
    digits = re.sub(r"\D", "", cleaned)
    return digits or None


def create_call_session(call_info: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_dirs()
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    caller = normalize_phone_number(call_info.get("incoming_number")) or "unknown"
    caller_slug = re.sub(r"[^0-9A-Za-z+]+", "-", caller).strip("-") or "unknown"
    session_id = f"{session_id}-{caller_slug}"
    meta = {
        "id": session_id,
        "started_at": utc_now_iso(),
        "ended_at": None,
        "state": call_info.get("state"),
        "incoming_number": normalize_phone_number(call_info.get("incoming_number")),
        "direction": call_info.get("direction"),
        "adb_serial": call_info.get("adb_serial"),
        "target_mac": call_info.get("target_mac"),
        "target_name": call_info.get("target_name"),
        "policy_action": None,
        "policy_reason": None,
        "summary": None,
    }
    if extra:
        meta.update(extra)
    session_dir = call_session_dir(session_id)
    save_json_file(session_dir / "meta.json", meta)
    save_current_call(meta)
    append_jsonl(session_dir / "events.jsonl", {"ts": utc_now_iso(), "type": "session_started", "call": call_info, "extra": extra or {}})
    return meta


def update_call_session(session_id: str, **changes: Any) -> dict[str, Any]:
    meta_path = call_session_dir(session_id) / "meta.json"
    meta = load_json_file(meta_path, {})
    meta.update(changes)
    save_json_file(meta_path, meta)
    current = load_current_call()
    if current and current.get("id") == session_id:
        current.update(changes)
        save_current_call(current)
    return meta


def append_call_event(session_id: str, event_type: str, **payload: Any) -> None:
    append_jsonl(call_session_dir(session_id) / "events.jsonl", {"ts": utc_now_iso(), "type": event_type, **payload})


def append_call_transcript(session_id: str, speaker: str, text: str, **payload: Any) -> None:
    append_jsonl(call_session_dir(session_id) / "transcript.jsonl", {"ts": utc_now_iso(), "speaker": speaker, "text": text, **payload})


def finalize_call_session(session_id: str, **changes: Any) -> dict[str, Any]:
    meta = update_call_session(session_id, ended_at=utc_now_iso(), state="idle", **changes)
    append_call_event(session_id, "session_finished", meta=meta)
    current = load_current_call()
    if current and current.get("id") == session_id:
        clear_current_call()
    return meta


def list_call_sessions(limit: int = 20) -> list[dict[str, Any]]:
    ensure_dirs()
    sessions = []
    for meta_path in sorted(CALLS_DIR.glob("*/meta.json"), reverse=True):
        try:
            sessions.append(load_json_file(meta_path, {}))
        except Exception:
            continue
    return sessions[:limit]


def read_call_session(session_id: str) -> dict[str, Any]:
    session_dir = call_session_dir(session_id)
    meta = load_json_file(session_dir / "meta.json", {})
    events = []
    transcript = []
    events_path = session_dir / "events.jsonl"
    transcript_path = session_dir / "transcript.jsonl"
    if events_path.exists():
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    if transcript_path.exists():
        transcript = [json.loads(line) for line in transcript_path.read_text().splitlines() if line.strip()]
    return {"meta": meta, "events": events, "transcript": transcript}


def pactl_json(kind: str) -> list[dict[str, Any]]:
    require_binary("pactl")
    result = run(["pactl", "-f", "json", "list", kind])
    return json.loads(result.stdout or "[]")


def list_sinks() -> list[dict[str, Any]]:
    return pactl_json("sinks")


def list_sources() -> list[dict[str, Any]]:
    return pactl_json("sources")


def list_cards() -> list[dict[str, Any]]:
    return pactl_json("cards")


def list_sink_inputs() -> list[dict[str, Any]]:
    return pactl_json("sink-inputs")


def list_source_outputs() -> list[dict[str, Any]]:
    return pactl_json("source-outputs")


def list_modules_short() -> list[dict[str, Any]]:
    require_binary("pactl")
    result = run(["pactl", "list", "short", "modules"])
    modules: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit():
            modules.append({"id": int(parts[0]), "name": parts[1], "args": "\t".join(parts[2:])})
    return modules


def get_default_sink() -> str:
    return run(["pactl", "get-default-sink"]).stdout.strip()


def get_default_source() -> str:
    return run(["pactl", "get-default-source"]).stdout.strip()


def sink_exists(name: str) -> bool:
    return any(s.get("name") == name for s in list_sinks())


def source_exists(name: str) -> bool:
    return any(s.get("name") == name for s in list_sources())


def wait_for_sink(name: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sink_exists(name):
            return True
        time.sleep(0.3)
    return False


def wait_for_source(name: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if source_exists(name):
            return True
        time.sleep(0.3)
    return False


def normalize_mac(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().replace("-", ":").replace("_", ":").upper()
    if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", cleaned):
        return cleaned
    return value.strip()


def mac_to_bluez(mac: str) -> str:
    return normalize_mac(mac).replace(":", "_")  # type: ignore[union-attr]


def extract_mac_from_name(name: str | None) -> str | None:
    if not name:
        return None
    match = re.search(r"([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})", name)
    if not match:
        return None
    return normalize_mac(match.group(1))


HFP_PROFILE_PATTERNS = [
    "headset-head-unit-msbc",
    "headset-head-unit-cvsd",
    "headset-head-unit",
    "handsfree_head_unit",
    "headset_head_unit",
    "hfp_hf",
    "hsp_hs",
]


def best_hfp_profile(card: dict[str, Any]) -> str | None:
    profiles = card.get("profiles") or {}
    available = {k: v for k, v in profiles.items() if v.get("available") is not False}
    if not available:
        return None
    for pattern in HFP_PROFILE_PATTERNS:
        for profile_name in available:
            if pattern in profile_name:
                return profile_name
    return None


def set_card_profile(card_name: str, profile: str) -> None:
    run(["pactl", "set-card-profile", card_name, profile])


def maybe_force_hfp(cards: list[dict[str, Any]], target_mac: str | None) -> None:
    candidates = []
    for card in cards:
        name = card.get("name", "")
        if not name.startswith("bluez_card."):
            continue
        mac = extract_mac_from_name(name)
        if target_mac and mac != target_mac:
            continue
        profile = best_hfp_profile(card)
        if not profile:
            continue
        if card.get("active_profile") == profile:
            continue
        candidates.append((card, profile, mac))
    if target_mac:
        chosen = candidates[:1]
    elif len(candidates) == 1:
        chosen = candidates
    else:
        chosen = []
    now = time.time()
    for card, profile, mac in chosen:
        key = card.get("name") or mac or "unknown"
        if now - LAST_FORCE_HFP.get(key, 0) < 10:
            continue
        try:
            log(f"switching {card.get('name')} to {profile}")
            set_card_profile(card.get("name"), profile)
        except CommandError as exc:
            log(f"failed to set HFP profile: {exc}")
        LAST_FORCE_HFP[key] = now


def bluez_pairs() -> list[dict[str, Any]]:
    sinks = [s for s in list_sinks() if str(s.get("name", "")).startswith("bluez_output.")]
    sources = [s for s in list_sources() if str(s.get("name", "")).startswith("bluez_input.") and not str(s.get("name", "")).endswith(".monitor")]
    grouped: dict[str, dict[str, Any]] = {}
    for sink in sinks:
        mac = extract_mac_from_name(sink.get("name"))
        if mac:
            grouped.setdefault(mac, {"mac": mac})["sink"] = sink
    for source in sources:
        mac = extract_mac_from_name(source.get("name"))
        if mac:
            grouped.setdefault(mac, {"mac": mac})["source"] = source
    pairs = []
    for mac, item in grouped.items():
        sink = item.get("sink")
        source = item.get("source")
        if sink and source:
            pairs.append(
                {
                    "mode": "device",
                    "mac": mac,
                    "sink": sink,
                    "source": source,
                    "sink_name": sink.get("name"),
                    "source_name": source.get("name"),
                    "description": sink.get("description") or source.get("description") or mac,
                }
            )
    pairs.sort(key=lambda item: str(item.get("description", "")))
    return pairs


def bluez_stream_pairs() -> list[dict[str, Any]]:
    sink_inputs = []
    for item in list_sink_inputs():
        props = item.get("properties") or {}
        name = str(props.get("node.name", ""))
        if name.startswith("bluez_input."):
            sink_inputs.append(item)
    source_outputs = []
    for item in list_source_outputs():
        props = item.get("properties") or {}
        name = str(props.get("node.name", ""))
        if name.startswith("bluez_output."):
            source_outputs.append(item)

    grouped: dict[str, dict[str, Any]] = {}
    for item in sink_inputs:
        props = item.get("properties") or {}
        name = props.get("node.name")
        mac = extract_mac_from_name(name) or normalize_mac(props.get("api.bluez5.address"))
        if mac:
            grouped.setdefault(mac, {"mac": mac})["sink_input"] = item
    for item in source_outputs:
        props = item.get("properties") or {}
        name = props.get("node.name")
        mac = extract_mac_from_name(name) or normalize_mac(props.get("api.bluez5.address"))
        if mac:
            grouped.setdefault(mac, {"mac": mac})["source_output"] = item

    pairs = []
    for mac, item in grouped.items():
        sink_input = item.get("sink_input")
        source_output = item.get("source_output")
        if sink_input and source_output:
            sink_props = sink_input.get("properties") or {}
            source_props = source_output.get("properties") or {}
            pairs.append(
                {
                    "mode": "stream",
                    "mac": mac,
                    "sink_input": sink_input,
                    "source_output": source_output,
                    "sink_input_index": sink_input.get("index"),
                    "source_output_index": source_output.get("index"),
                    "source_name": sink_props.get("node.name"),
                    "sink_name": source_props.get("node.name"),
                    "description": sink_props.get("device.description") or source_props.get("device.description") or mac,
                }
            )
    pairs.sort(key=lambda item: str(item.get("description", "")))
    return pairs


def available_bluez_pairs() -> list[dict[str, Any]]:
    pairs = bluez_pairs()
    return pairs if pairs else bluez_stream_pairs()


def choose_pair(target_mac: str | None) -> dict[str, Any] | None:
    pairs = available_bluez_pairs()
    if target_mac:
        for pair in pairs:
            if pair["mac"] == target_mac:
                return pair
        return None
    return pairs[0] if pairs else None


def pulse_module_load(module: str, **kwargs: Any) -> int:
    args = ["pactl", "load-module", module]
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        args.append(f"{key}={value}")
    result = run(args)
    return int(result.stdout.strip())


def pulse_module_unload(module_id: int | None) -> None:
    if module_id is None:
        return
    try:
        run(["pactl", "unload-module", str(module_id)], check=False)
    except CommandError:
        pass


def move_sink_input(index: int | None, sink: str | None) -> None:
    if index is None or not sink:
        return
    run(["pactl", "move-sink-input", str(index), sink])


def move_source_output(index: int | None, source: str | None) -> None:
    if index is None or not source:
        return
    run(["pactl", "move-source-output", str(index), source])


def find_named_sink(name: str) -> dict[str, Any] | None:
    for sink in list_sinks():
        if sink.get("name") == name:
            return sink
    return None


def find_named_source(name: str) -> dict[str, Any] | None:
    for source in list_sources():
        if source.get("name") == name:
            return source
    return None


def find_sink_input(index: int) -> dict[str, Any] | None:
    for item in list_sink_inputs():
        if int(item.get("index", -1)) == int(index):
            return item
    return None


def find_source_output(index: int) -> dict[str, Any] | None:
    for item in list_source_outputs():
        if int(item.get("index", -1)) == int(index):
            return item
    return None


class BridgeController:
    def __init__(self) -> None:
        self.state = load_state()

    def cleanup(self) -> None:
        state = load_state()
        if state.get("mode") == "stream":
            default_sink = None
            default_source = None
            try:
                default_sink = get_default_sink()
            except Exception:
                pass
            try:
                default_source = get_default_source()
            except Exception:
                pass
            try:
                move_sink_input(state.get("sink_input_id"), default_sink)
            except Exception:
                pass
            try:
                move_source_output(state.get("source_output_id"), default_source)
            except Exception:
                pass
        pulse_module_unload(state.get("loopback_rx_id"))
        pulse_module_unload(state.get("loopback_tx_id"))
        pulse_module_unload(state.get("echo_module_id"))
        clear_state()

    def bridge_signature(
        self,
        phone_source: str,
        phone_sink: str,
        local_source: str,
        local_sink: str,
        echo_cancel: bool,
        target_mac: str | None,
        latency_msec: int,
    ) -> dict[str, Any]:
        return {
            "phone_source": phone_source,
            "phone_sink": phone_sink,
            "local_source": local_source,
            "local_sink": local_sink,
            "echo_cancel": echo_cancel,
            "target_mac": target_mac,
            "latency_msec": latency_msec,
        }

    def ensure(
        self,
        phone_source: str,
        phone_sink: str,
        local_source: str,
        local_sink: str,
        echo_cancel: bool,
        target_mac: str | None,
        latency_msec: int,
    ) -> None:
        desired = self.bridge_signature(
            phone_source, phone_sink, local_source, local_sink, echo_cancel, target_mac, latency_msec
        )
        current = load_state().get("signature")
        if current == desired:
            return
        self.cleanup()
        echo_id = None
        tx_source = local_source
        rx_sink = local_sink
        if echo_cancel:
            log(f"loading echo canceller: {local_source} -> {local_sink}")
            echo_id = pulse_module_load(
                "module-echo-cancel",
                source_name=ECHO_SOURCE_NAME,
                sink_name=ECHO_SINK_NAME,
                source_master=local_source,
                sink_master=local_sink,
                aec_method="webrtc",
            )
            if not wait_for_source(ECHO_SOURCE_NAME) or not wait_for_sink(ECHO_SINK_NAME):
                pulse_module_unload(echo_id)
                raise CommandError("echo-cancel nodes did not appear in time")
            tx_source = ECHO_SOURCE_NAME
            rx_sink = ECHO_SINK_NAME
        log(f"loading loopback RX: {phone_source} -> {rx_sink}")
        rx_id = pulse_module_load(
            "module-loopback",
            source=phone_source,
            sink=rx_sink,
            latency_msec=latency_msec,
            source_dont_move=True,
            sink_dont_move=True,
            remix=True,
        )
        log(f"loading loopback TX: {tx_source} -> {phone_sink}")
        tx_id = pulse_module_load(
            "module-loopback",
            source=tx_source,
            sink=phone_sink,
            latency_msec=latency_msec,
            source_dont_move=True,
            sink_dont_move=True,
            remix=True,
        )
        save_state(
            {
                "mode": "device",
                "echo_module_id": echo_id,
                "loopback_rx_id": rx_id,
                "loopback_tx_id": tx_id,
                "signature": desired,
                "created_at": int(time.time()),
            }
        )

    def ensure_stream_targets(self, sink_input_id: int, source_output_id: int, rx_sink: str, tx_source: str) -> bool:
        sink = find_named_sink(rx_sink)
        source = find_named_source(tx_source)
        sink_input = find_sink_input(sink_input_id)
        source_output = find_source_output(source_output_id)
        if not sink or not source or not sink_input or not source_output:
            return False
        return int(sink_input.get("sink", -1)) == int(sink.get("index", -2)) and int(source_output.get("source", -1)) == int(source.get("index", -2))

    def ensure_streams(
        self,
        sink_input_id: int,
        source_output_id: int,
        phone_source: str,
        phone_sink: str,
        local_source: str,
        local_sink: str,
        echo_cancel: bool,
        target_mac: str | None,
        latency_msec: int,
    ) -> None:
        desired = {
            "mode": "stream",
            "sink_input_id": sink_input_id,
            "source_output_id": source_output_id,
            "phone_source": phone_source,
            "phone_sink": phone_sink,
            "local_source": local_source,
            "local_sink": local_sink,
            "echo_cancel": echo_cancel,
            "target_mac": target_mac,
            "latency_msec": latency_msec,
        }
        current = load_state().get("signature")
        desired_rx_sink = ECHO_SINK_NAME if echo_cancel else local_sink
        desired_tx_source = ECHO_SOURCE_NAME if echo_cancel else local_source
        if current == desired and self.ensure_stream_targets(sink_input_id, source_output_id, desired_rx_sink, desired_tx_source):
            return
        self.cleanup()
        echo_id = None
        tx_source = local_source
        rx_sink = local_sink
        if echo_cancel:
            log(f"loading echo canceller: {local_source} -> {local_sink}")
            echo_id = pulse_module_load(
                "module-echo-cancel",
                source_name=ECHO_SOURCE_NAME,
                sink_name=ECHO_SINK_NAME,
                source_master=local_source,
                sink_master=local_sink,
                aec_method="webrtc",
            )
            if not wait_for_source(ECHO_SOURCE_NAME) or not wait_for_sink(ECHO_SINK_NAME):
                pulse_module_unload(echo_id)
                raise CommandError("echo-cancel nodes did not appear in time")
            tx_source = ECHO_SOURCE_NAME
            rx_sink = ECHO_SINK_NAME
        try:
            log(f"moving phone RX stream {sink_input_id} -> {rx_sink}")
            move_sink_input(sink_input_id, rx_sink)
            log(f"moving phone TX stream {source_output_id} -> {tx_source}")
            move_source_output(source_output_id, tx_source)
        except Exception:
            pulse_module_unload(echo_id)
            raise
        save_state(
            {
                "mode": "stream",
                "echo_module_id": echo_id,
                "sink_input_id": sink_input_id,
                "source_output_id": source_output_id,
                "signature": desired,
                "created_at": int(time.time()),
            }
        )


def bluetoothctl(*commands: str, check: bool = True) -> str:
    require_binary("bluetoothctl")
    joined = "\n".join(commands + ("quit",)) + "\n"
    result = subprocess.run(["bluetoothctl"], input=joined, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise CommandError(result.stderr.strip() or result.stdout.strip() or "bluetoothctl failed")
    return result.stdout


def parse_bt_devices(text: str) -> list[dict[str, str]]:
    devices = []
    for line in text.splitlines():
        match = re.match(r"Device\s+([0-9A-F:]{17})\s+(.+)$", line.strip())
        if match:
            devices.append({"mac": match.group(1), "name": match.group(2)})
    return devices


def parse_adb_devices(text: str) -> list[dict[str, str]]:
    devices = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        row = {"serial": parts[0], "state": parts[1]}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                row[key] = value
        devices.append(row)
    return devices


def adb_devices(cli_serial: str | None = None) -> list[dict[str, str]]:
    require_binary("adb")
    cmd = ["adb", "devices", "-l"]
    if cli_serial:
        cmd = ["adb", "-s", cli_serial, "devices", "-l"]
    return parse_adb_devices(run(cmd, check=False).stdout)


def connected_adb_devices() -> list[dict[str, str]]:
    return [device for device in adb_devices() if device.get("state") == "device"]


def caller_number_from_text(text: str) -> str | None:
    patterns = [
        r"mCallIncomingNumber=([^\s]+)",
        r"handle:\s*tel:([+0-9][0-9 -]+)",
        r"address:\s*tel:([+0-9][0-9 -]+)",
        r"tel:([+0-9][0-9 -]{5,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            number = normalize_phone_number(match.group(1))
            if number:
                return number
    return None


def load_bindings(cfg: dict[str, Any]) -> dict[str, Any]:
    bindings = cfg.get("device_bindings") or {}
    return bindings if isinstance(bindings, dict) else {}


def save_binding(cfg: dict[str, Any], adb_serial_value: str, mac: str, bt_name: str | None = None, model: str | None = None) -> bool:
    bindings = load_bindings(cfg)
    current = bindings.get(adb_serial_value) or {}
    desired = {"mac": mac, "bt_name": bt_name, "model": model}
    if current == desired:
        return False
    bindings[adb_serial_value] = desired
    cfg["device_bindings"] = bindings
    save_config(cfg)
    return True


def normalize_name_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    cleaned = re.sub(r"[^0-9a-z]+", " ", value.lower())
    return {token for token in cleaned.split() if len(token) >= 2}


def match_adb_to_pair(adb_device: dict[str, str], pairs: list[dict[str, Any]], cfg: dict[str, Any]) -> str | None:
    serial = adb_device.get("serial")
    bindings = load_bindings(cfg)
    if serial and serial in bindings:
        bound_mac = normalize_mac((bindings.get(serial) or {}).get("mac"))
        if bound_mac and any(pair["mac"] == bound_mac for pair in pairs):
            return bound_mac
    model_tokens = normalize_name_tokens(adb_device.get("model") or adb_device.get("device") or adb_device.get("product"))
    if model_tokens:
        for pair in pairs:
            name_tokens = normalize_name_tokens(pair.get("description"))
            if model_tokens and model_tokens <= name_tokens:
                return pair["mac"]
        for pair in pairs:
            name_tokens = normalize_name_tokens(pair.get("description"))
            if model_tokens & name_tokens:
                return pair["mac"]
    return None


def resolve_target_mac(cfg: dict[str, Any], cli_target_mac: str | None = None, pairs: list[dict[str, Any]] | None = None) -> str | None:
    if cli_target_mac:
        return normalize_mac(cli_target_mac)
    if cfg.get("target_device"):
        return normalize_mac(cfg.get("target_device"))
    if cfg.get("auto_select_device") is False:
        return None
    pairs = pairs if pairs is not None else available_bluez_pairs()
    if len(pairs) == 1:
        return pairs[0]["mac"]
    connected_bt = parse_bt_devices(bluetoothctl("devices Connected", check=False))
    if len(connected_bt) == 1:
        return normalize_mac(connected_bt[0]["mac"])
    connected_adb = connected_adb_devices()
    if len(connected_adb) == 1 and pairs:
        matched = match_adb_to_pair(connected_adb[0], pairs, cfg)
        if matched:
            if save_binding(cfg, connected_adb[0]["serial"], matched, next((p.get("description") for p in pairs if p["mac"] == matched), None), connected_adb[0].get("model")):
                log(f"learned device binding: {connected_adb[0]['serial']} -> {matched}")
            return matched
    return None


def adb_serial(cli_serial: str | None, cfg: dict[str, Any], target_mac: str | None = None) -> str | None:
    if cli_serial:
        return cli_serial
    configured = cfg.get("adb_serial")
    if configured:
        return configured
    devices = connected_adb_devices()
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0]["serial"]
    bindings = load_bindings(cfg)
    if target_mac:
        for serial, binding in bindings.items():
            if normalize_mac((binding or {}).get("mac")) == normalize_mac(target_mac) and any(device.get("serial") == serial for device in devices):
                return serial
    return None


def adb_cmd(extra: list[str], cli_serial: str | None, cfg: dict[str, Any], target_mac: str | None = None) -> None:
    require_binary("adb")
    serial = adb_serial(cli_serial, cfg, target_mac=target_mac)
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += extra
    run(cmd)


def adb_capture(extra: list[str], cli_serial: str | None, cfg: dict[str, Any], target_mac: str | None = None) -> str:
    require_binary("adb")
    serial = adb_serial(cli_serial, cfg, target_mac=target_mac)
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += extra
    return run(cmd).stdout


def android_call_info(cli_serial: str | None, cfg: dict[str, Any], target_mac: str | None = None) -> dict[str, Any]:
    serial = adb_serial(cli_serial, cfg, target_mac=target_mac)
    telephony = adb_capture(["shell", "dumpsys", "telephony.registry"], serial, cfg, target_mac=target_mac)
    telecom = adb_capture(["shell", "dumpsys", "telecom"], serial, cfg, target_mac=target_mac)
    states = [int(match.group(1)) for match in re.finditer(r"\bmCallState=(\d+)", telephony)]
    state = None
    if states:
        if any(item == 1 for item in states):
            state = "ringing"
        elif any(item == 2 for item in states):
            state = "offhook"
        else:
            state = "idle"
    direction = None
    if state in {"ringing", "offhook"}:
        if re.search(r"\bINCOMING\b|MT - incoming", telecom, flags=re.IGNORECASE):
            direction = "incoming"
        elif re.search(r"\bOUTGOING\b|MO - outgoing", telecom, flags=re.IGNORECASE):
            direction = "outgoing"
    number = caller_number_from_text(telephony) or (caller_number_from_text(telecom) if state in {"ringing", "offhook"} else None)
    return {
        "state": state,
        "incoming_number": number,
        "direction": direction,
        "adb_serial": serial,
    }


def android_call_state(cli_serial: str | None, cfg: dict[str, Any], target_mac: str | None = None) -> str | None:
    return android_call_info(cli_serial, cfg, target_mac=target_mac).get("state")


def bluetooth_connected(target_mac: str | None) -> bool:
    connected = parse_bt_devices(bluetoothctl("devices Connected", check=False))
    if target_mac:
        return any(device["mac"] == target_mac for device in connected)
    return bool(connected)


def parse_business_days(value: str | list[str] | None) -> list[str]:
    if value is None:
        return WEEKDAY_NAMES[:]
    if isinstance(value, list):
        items = value
    else:
        items = [item.strip().lower() for item in value.split(",") if item.strip()]
    days = [item for item in items if item in WEEKDAY_NAMES]
    return days or WEEKDAY_NAMES[:]


def within_business_hours(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    hours = cfg.get("business_hours")
    if not hours:
        return True
    now = now or datetime.now()
    allowed_days = parse_business_days(cfg.get("business_days"))
    if WEEKDAY_NAMES[now.weekday()] not in allowed_days:
        return False
    match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", str(hours).strip())
    if not match:
        return True
    start_minutes = int(match.group(1)) * 60 + int(match.group(2))
    end_minutes = int(match.group(3)) * 60 + int(match.group(4))
    current_minutes = now.hour * 60 + now.minute
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def number_matches_patterns(number: str | None, patterns: list[str]) -> bool:
    normalized = normalize_phone_number(number)
    if not normalized:
        return False
    digits = normalized.lstrip("+")
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        normalized_pattern = normalize_phone_number(pattern)
        if normalized_pattern and (normalized == normalized_pattern or digits.endswith(normalized_pattern.lstrip("+"))):
            return True
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(digits, pattern.lstrip("+")):
            return True
    return False


def evaluate_call_policy(cfg: dict[str, Any], call_info: dict[str, Any]) -> dict[str, str]:
    auto_reject = bool(cfg.get("auto_reject_blocked", False))
    outside_hours = not within_business_hours(cfg)
    if outside_hours:
        return {"action": "reject" if auto_reject else "ignore", "reason": "outside_business_hours"}
    mode = str(cfg.get("call_policy_mode") or "allow_all")
    caller = normalize_phone_number(call_info.get("incoming_number"))
    allowed = cfg.get("allowed_callers") or []
    blocked = cfg.get("blocked_callers") or []
    unknown_mode = str(cfg.get("unknown_callers") or "allow")

    if mode == "allowlist":
        if caller and number_matches_patterns(caller, allowed):
            return {"action": "answer", "reason": "allowlist_match"}
        return {"action": "reject" if auto_reject else "ignore", "reason": "allowlist_miss" if caller else "unknown_caller"}

    if mode == "blocklist":
        if caller and number_matches_patterns(caller, blocked):
            return {"action": "reject" if auto_reject else "ignore", "reason": "blocklist_match"}
        if caller is None and unknown_mode == "deny":
            return {"action": "reject" if auto_reject else "ignore", "reason": "unknown_caller"}
        return {"action": "answer", "reason": "blocklist_pass"}

    if caller is None and unknown_mode == "deny":
        return {"action": "reject" if auto_reject else "ignore", "reason": "unknown_caller"}
    return {"action": "answer", "reason": "allow_all"}


def caller_lists_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "call_policy_mode": cfg.get("call_policy_mode"),
        "allowed_callers": cfg.get("allowed_callers") or [],
        "blocked_callers": cfg.get("blocked_callers") or [],
        "unknown_callers": cfg.get("unknown_callers"),
        "business_hours": cfg.get("business_hours"),
        "business_days": parse_business_days(cfg.get("business_days")),
        "auto_reject_blocked": bool(cfg.get("auto_reject_blocked", False)),
    }


def configure_cmd(args: argparse.Namespace) -> None:
    cfg = load_config()
    changed = False
    if args.device is not None:
        cfg["target_device"] = normalize_mac(args.device)
        changed = True
    if args.clear_device:
        cfg["target_device"] = None
        changed = True
    if args.sink is not None:
        cfg["local_sink"] = args.sink
        changed = True
    if args.source is not None:
        cfg["local_source"] = args.source
        changed = True
    if args.echo_cancel is not None:
        cfg["echo_cancel"] = args.echo_cancel == "on"
        changed = True
    if args.latency is not None:
        cfg["latency_msec"] = int(args.latency)
        changed = True
    if args.adb_serial is not None:
        cfg["adb_serial"] = args.adb_serial
        changed = True
    if args.clear_adb_serial:
        cfg["adb_serial"] = None
        changed = True
    if args.auto_answer is not None:
        cfg["auto_answer"] = args.auto_answer == "on"
        changed = True
    if args.auto_answer_delay is not None:
        cfg["auto_answer_delay_sec"] = max(0, int(args.auto_answer_delay))
        changed = True
    if args.auto_select_device is not None:
        cfg["auto_select_device"] = args.auto_select_device == "on"
        changed = True
    if args.policy_mode is not None:
        cfg["call_policy_mode"] = args.policy_mode
        changed = True
    if args.unknown_callers is not None:
        cfg["unknown_callers"] = args.unknown_callers
        changed = True
    if args.business_hours is not None:
        cfg["business_hours"] = args.business_hours
        changed = True
    if args.clear_business_hours:
        cfg["business_hours"] = None
        changed = True
    if args.business_days is not None:
        cfg["business_days"] = parse_business_days(args.business_days)
        changed = True
    if args.auto_reject is not None:
        cfg["auto_reject_blocked"] = args.auto_reject == "on"
        changed = True
    if args.log_calls is not None:
        cfg["log_calls"] = args.log_calls == "on"
        changed = True
    allowed = list(cfg.get("allowed_callers") or [])
    blocked = list(cfg.get("blocked_callers") or [])
    if getattr(args, "allow_caller", None):
        for item in args.allow_caller:
            if item not in allowed:
                allowed.append(item)
        cfg["allowed_callers"] = allowed
        changed = True
    if getattr(args, "remove_allow_caller", None):
        cfg["allowed_callers"] = [item for item in allowed if item not in set(args.remove_allow_caller)]
        changed = True
    if args.clear_allowed_callers:
        cfg["allowed_callers"] = []
        changed = True
    if getattr(args, "block_caller", None):
        for item in args.block_caller:
            if item not in blocked:
                blocked.append(item)
        cfg["blocked_callers"] = blocked
        changed = True
    if getattr(args, "remove_block_caller", None):
        cfg["blocked_callers"] = [item for item in blocked if item not in set(args.remove_block_caller)]
        changed = True
    if args.clear_blocked_callers:
        cfg["blocked_callers"] = []
        changed = True
    if args.clear_bindings:
        cfg["device_bindings"] = {}
        changed = True
    if not changed:
        print(json.dumps(cfg, indent=2))
        return
    save_config(cfg)
    print(json.dumps(cfg, indent=2))


def print_status() -> None:
    cfg = load_config()
    state = load_state()
    pairs = available_bluez_pairs()
    resolved_target = resolve_target_mac(cfg, pairs=pairs)
    resolved_serial = adb_serial(None, cfg, target_mac=resolved_target)
    info = {
        "config": cfg,
        "state": state,
        "wireplumber_config": str(WIREPLUMBER_CONFIG_PATH),
        "wireplumber_config_exists": WIREPLUMBER_CONFIG_PATH.exists(),
        "default_sink": safe_value(get_default_sink),
        "default_source": safe_value(get_default_source),
        "selected_target_device": resolved_target,
        "selected_adb_serial": resolved_serial,
        "call_policy": caller_lists_snapshot(cfg),
        "active_call_session": load_current_call(),
        "bluez_pairs": bluez_pairs_safe(),
        "bluez_cards": bluez_cards_safe(),
        "bt_connected": parse_bt_devices(safe_text(lambda: bluetoothctl("devices Connected", check=False))),
        "bt_paired": parse_bt_devices(safe_text(lambda: bluetoothctl("devices Paired", check=False))),
        "adb_devices": adb_devices_safe(),
        "adb_devices_detailed": safe_value(connected_adb_devices),
        "adb_call_info": safe_value(lambda: android_call_info(None, cfg, target_mac=resolved_target)) if resolved_serial else None,
        "callscoot_modules": [m for m in list_modules_short_safe() if "callscoot" in m.get("args", "")],
        "service_active": safe_text(lambda: run(["systemctl", "--user", "is-active", SERVICE_NAME], check=False).stdout).strip(),
    }
    print(json.dumps(info, indent=2))


def safe_value(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def safe_text(fn) -> str:
    try:
        value = fn()
        return value if isinstance(value, str) else str(value)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


def bluez_pairs_safe() -> list[dict[str, Any]]:
    try:
        rows = []
        for p in available_bluez_pairs():
            row = {
                "mode": p.get("mode"),
                "mac": p["mac"],
                "source": p.get("source_name"),
                "sink": p.get("sink_name"),
                "description": p.get("description"),
            }
            if p.get("mode") == "stream":
                row["sink_input_index"] = p.get("sink_input_index")
                row["source_output_index"] = p.get("source_output_index")
            rows.append(row)
        return rows
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]


def bluez_cards_safe() -> list[dict[str, Any]]:
    try:
        cards = []
        for card in list_cards():
            name = card.get("name", "")
            if name.startswith("bluez_card."):
                cards.append(
                    {
                        "name": name,
                        "active_profile": card.get("active_profile"),
                        "best_hfp_profile": best_hfp_profile(card),
                    }
                )
        return cards
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]


def adb_devices_safe() -> list[str]:
    try:
        require_binary("adb")
        out = run(["adb", "devices"], check=False).stdout
        return [line for line in out.splitlines()[1:] if line.strip()]
    except Exception as exc:  # noqa: BLE001
        return [f"ERROR: {exc}"]


def list_modules_short_safe() -> list[dict[str, Any]]:
    try:
        return list_modules_short()
    except Exception:
        return []


def pair_mode(args: argparse.Namespace) -> None:
    timeout = int(args.timeout)
    before = {item["mac"] for item in parse_bt_devices(bluetoothctl("devices Paired", check=False))}
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["bluetoothctl", "--agent=KeyboardDisplay"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=False,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        for command in [
            "power on\n",
            "pairable on\n",
            f"discoverable-timeout {timeout}\n",
            "discoverable on\n",
        ]:
            os.write(master_fd, command.encode())
            time.sleep(0.3)

        log(f"pairing window open for {timeout}s")
        print("Open Bluetooth settings on the phone and pair with this laptop now.")
        deadline = time.time() + timeout
        seen = set(before)
        while time.time() < deadline:
            devices = parse_bt_devices(bluetoothctl("devices Paired", check=False))
            for device in devices:
                if device["mac"] not in seen:
                    seen.add(device["mac"])
                    print(f"paired: {device['mac']}  {device['name']}")
            if proc.poll() is not None:
                break
            time.sleep(2)
    finally:
        bluetoothctl("discoverable off", "pairable off", check=False)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    after = parse_bt_devices(bluetoothctl("devices Paired", check=False))
    print(json.dumps(after, indent=2))


def trust_cmd(args: argparse.Namespace) -> None:
    mac = normalize_mac(args.mac)
    print(bluetoothctl(f"trust {mac}"))


def connect_cmd(args: argparse.Namespace) -> None:
    mac = normalize_mac(args.mac)
    print(bluetoothctl(f"connect {mac}"))


def devices_cmd(_: argparse.Namespace) -> None:
    paired = parse_bt_devices(bluetoothctl("devices Paired", check=False))
    connected = {d["mac"] for d in parse_bt_devices(bluetoothctl("devices Connected", check=False))}
    rows = []
    for device in paired:
        device["connected"] = device["mac"] in connected
        rows.append(device)
    print(json.dumps(rows, indent=2))


def resolve_local_endpoints(cfg: dict[str, Any], args: argparse.Namespace) -> tuple[str, str]:
    sink = args.sink or cfg.get("local_sink") or get_default_sink()
    source = args.source or cfg.get("local_source") or get_default_source()
    return source, sink


def bridge_up(args: argparse.Namespace) -> None:
    cfg = load_config()
    pairs = available_bluez_pairs()
    target_mac = resolve_target_mac(cfg, args.device, pairs=pairs)
    cards = list_cards()
    maybe_force_hfp(cards, target_mac)
    pair = choose_pair(target_mac)
    if not pair:
        raise CommandError("no active Bluetooth call-audio route found")
    local_source, local_sink = resolve_local_endpoints(cfg, args)
    echo_cancel = cfg.get("echo_cancel", True) if args.echo_cancel is None else args.echo_cancel == "on"
    latency_msec = int(args.latency or cfg.get("latency_msec") or 60)
    controller = BridgeController()
    if pair.get("mode") == "stream":
        controller.ensure_streams(
            sink_input_id=int(pair["sink_input_index"]),
            source_output_id=int(pair["source_output_index"]),
            phone_source=pair["source_name"],
            phone_sink=pair["sink_name"],
            local_source=local_source,
            local_sink=local_sink,
            echo_cancel=echo_cancel,
            target_mac=pair["mac"],
            latency_msec=latency_msec,
        )
    else:
        controller.ensure(
            phone_source=pair["source_name"],
            phone_sink=pair["sink_name"],
            local_source=local_source,
            local_sink=local_sink,
            echo_cancel=echo_cancel,
            target_mac=pair["mac"],
            latency_msec=latency_msec,
        )
    print(
        json.dumps(
            {
                "target": pair,
                "local_source": local_source,
                "local_sink": local_sink,
                "echo_cancel": echo_cancel,
                "latency_msec": latency_msec,
                "state": load_state(),
            },
            indent=2,
        )
    )


def bridge_down(_: argparse.Namespace) -> None:
    BridgeController().cleanup()
    print(json.dumps({"stopped": True}, indent=2))


def daemon_cmd(args: argparse.Namespace) -> None:
    global STOP
    controller = BridgeController()
    last_call_state: str | None = None
    ring_started_at: float | None = None
    ring_action_taken = False
    ring_policy_logged = False

    def handle_signal(signum, _frame):
        global STOP
        STOP = True
        log(f"signal {signum} received, shutting down")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("daemon started")
    while not STOP:
        try:
            cfg = load_config()
            pairs = available_bluez_pairs()
            target_mac = resolve_target_mac(cfg, args.device, pairs=pairs)
            selected_adb_serial = adb_serial(None, cfg, target_mac=target_mac)
            echo_cancel = cfg.get("echo_cancel", True) if args.echo_cancel is None else args.echo_cancel == "on"
            latency_msec = int(args.latency or cfg.get("latency_msec") or 60)
            local_source_override = args.source or cfg.get("local_source")
            local_sink_override = args.sink or cfg.get("local_sink")
            auto_answer = cfg.get("auto_answer", False) if getattr(args, "auto_answer", None) is None else args.auto_answer == "on"
            auto_answer_delay = max(0, int(getattr(args, "auto_answer_delay", None) or cfg.get("auto_answer_delay_sec") or 0))
            log_calls = bool(cfg.get("log_calls", True))

            call_info = {"state": None, "incoming_number": None, "direction": None, "adb_serial": selected_adb_serial}
            if selected_adb_serial:
                call_info = android_call_info(None, cfg, target_mac=target_mac)
            pair = choose_pair(target_mac)
            call_info["target_mac"] = target_mac or (pair.get("mac") if pair else None)
            call_info["target_name"] = pair.get("description") if pair else None
            call_state = call_info.get("state")
            current_session = load_current_call()

            if call_state != last_call_state:
                if log_calls and call_state in {"ringing", "offhook"} and not current_session:
                    current_session = create_call_session(call_info, extra={"policy": caller_lists_snapshot(cfg)})
                if current_session:
                    session_id = current_session["id"]
                    update_call_session(
                        session_id,
                        state=call_state,
                        incoming_number=normalize_phone_number(call_info.get("incoming_number")),
                        direction=call_info.get("direction"),
                        adb_serial=call_info.get("adb_serial"),
                        target_mac=call_info.get("target_mac"),
                        target_name=call_info.get("target_name"),
                    )
                    append_call_event(session_id, "call_state_changed", previous_state=last_call_state, state=call_state, call=call_info)
                if call_state == "ringing":
                    ring_started_at = time.time()
                    ring_action_taken = False
                    ring_policy_logged = False
                    log("incoming call detected")
                else:
                    ring_started_at = None
                    ring_action_taken = False
                    ring_policy_logged = False
                if last_call_state in {"ringing", "offhook"} and call_state == "idle" and current_session:
                    finalize_call_session(
                        current_session["id"],
                        incoming_number=normalize_phone_number(call_info.get("incoming_number")),
                        direction=call_info.get("direction"),
                        adb_serial=call_info.get("adb_serial"),
                        target_mac=call_info.get("target_mac"),
                        target_name=call_info.get("target_name"),
                    )
                    current_session = None
                last_call_state = call_state
            elif log_calls and call_state in {"ringing", "offhook"} and not current_session:
                current_session = create_call_session(call_info, extra={"policy": caller_lists_snapshot(cfg)})

            if auto_answer and call_state == "ringing" and selected_adb_serial:
                decision = evaluate_call_policy(cfg, call_info)
                if current_session and not ring_policy_logged:
                    append_call_event(current_session["id"], "call_policy", decision=decision, call=call_info)
                    update_call_session(current_session["id"], policy_action=decision["action"], policy_reason=decision["reason"])
                    ring_policy_logged = True
                bluetooth_ready = bluetooth_connected(target_mac)
                if decision["action"] == "reject" and not ring_action_taken:
                    log(f"auto-rejecting incoming call: {decision['reason']}")
                    adb_cmd(["shell", "input", "keyevent", "KEYCODE_ENDCALL"], None, cfg, target_mac=target_mac)
                    ring_action_taken = True
                elif decision["action"] == "answer" and not ring_action_taken and ring_started_at is not None:
                    if time.time() - ring_started_at >= auto_answer_delay and bluetooth_ready:
                        log("auto-answering incoming call over ADB")
                        adb_cmd(["shell", "input", "keyevent", "KEYCODE_HEADSETHOOK"], None, cfg, target_mac=target_mac)
                        ring_action_taken = True

            cards = list_cards()
            maybe_force_hfp(cards, target_mac)
            pair = choose_pair(target_mac)
            if not pair:
                controller.cleanup()
                time.sleep(2)
                continue
            local_source = local_source_override or get_default_source()
            local_sink = local_sink_override or get_default_sink()
            if pair.get("mode") == "stream":
                controller.ensure_streams(
                    sink_input_id=int(pair["sink_input_index"]),
                    source_output_id=int(pair["source_output_index"]),
                    phone_source=pair["source_name"],
                    phone_sink=pair["sink_name"],
                    local_source=local_source,
                    local_sink=local_sink,
                    echo_cancel=echo_cancel,
                    target_mac=pair["mac"],
                    latency_msec=latency_msec,
                )
            else:
                controller.ensure(
                    phone_source=pair["source_name"],
                    phone_sink=pair["sink_name"],
                    local_source=local_source,
                    local_sink=local_sink,
                    echo_cancel=echo_cancel,
                    target_mac=pair["mac"],
                    latency_msec=latency_msec,
                )
        except Exception as exc:  # noqa: BLE001
            log(f"daemon error: {exc}")
            time.sleep(2)
        else:
            time.sleep(1)
    controller.cleanup()


def logs_cmd(args: argparse.Namespace) -> None:
    cmd = ["journalctl", "--user", "-u", SERVICE_NAME, "-n", str(args.lines)]
    if args.follow:
        cmd.append("-f")
    subprocess.run(cmd, check=False)


def calls_cmd(args: argparse.Namespace) -> None:
    print(json.dumps(list_call_sessions(limit=args.lines), indent=2))


def call_show_cmd(args: argparse.Namespace) -> None:
    print(json.dumps(read_call_session(args.session_id), indent=2))


def dial_cmd(args: argparse.Namespace) -> None:
    cfg = load_config()
    target_mac = resolve_target_mac(cfg)
    adb_cmd(["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{args.number}"], args.serial, cfg, target_mac=target_mac)


def hangup_cmd(args: argparse.Namespace) -> None:
    cfg = load_config()
    target_mac = resolve_target_mac(cfg)
    adb_cmd(["shell", "input", "keyevent", "KEYCODE_ENDCALL"], args.serial, cfg, target_mac=target_mac)


def answer_cmd(args: argparse.Namespace) -> None:
    cfg = load_config()
    target_mac = resolve_target_mac(cfg)
    adb_cmd(["shell", "input", "keyevent", "KEYCODE_HEADSETHOOK"], args.serial, cfg, target_mac=target_mac)


def wireplumber_config_text() -> str:
    return """wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}

monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source hsp_hs hfp_hf ]
  bluez5.hfphsp-backend = \"native\"
  bluez5.enable-msbc = true
}
"""


def install_user_cmd(_: argparse.Namespace) -> None:
    ensure_dirs()
    WIREPLUMBER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    WIREPLUMBER_CONFIG_PATH.write_text(wireplumber_config_text())
    cfg = load_config()
    save_config(cfg)
    print(json.dumps({"wireplumber_config": str(WIREPLUMBER_CONFIG_PATH), "config": str(CONFIG_PATH)}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP, description="Bluetooth phone audio bridge for Linux + Android")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show current CallScoot / Bluetooth / PipeWire state")
    p.set_defaults(func=lambda args: print_status())

    p = sub.add_parser("devices", help="list paired Bluetooth devices")
    p.set_defaults(func=devices_cmd)

    p = sub.add_parser("pair", help="open a temporary Bluetooth pairing window")
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=pair_mode)

    p = sub.add_parser("trust", help="trust a paired Bluetooth device")
    p.add_argument("mac")
    p.set_defaults(func=trust_cmd)

    p = sub.add_parser("connect", help="connect a paired Bluetooth device")
    p.add_argument("mac")
    p.set_defaults(func=connect_cmd)

    p = sub.add_parser("configure", help="show or update CallScoot config")
    p.add_argument("--device")
    p.add_argument("--clear-device", action="store_true")
    p.add_argument("--sink")
    p.add_argument("--source")
    p.add_argument("--echo-cancel", choices=["on", "off"])
    p.add_argument("--latency", type=int)
    p.add_argument("--adb-serial")
    p.add_argument("--clear-adb-serial", action="store_true")
    p.add_argument("--auto-answer", choices=["on", "off"])
    p.add_argument("--auto-answer-delay", type=int)
    p.add_argument("--auto-select-device", choices=["on", "off"])
    p.add_argument("--policy-mode", choices=["allow_all", "allowlist", "blocklist"])
    p.add_argument("--allow-caller", action="append")
    p.add_argument("--remove-allow-caller", action="append")
    p.add_argument("--clear-allowed-callers", action="store_true")
    p.add_argument("--block-caller", action="append")
    p.add_argument("--remove-block-caller", action="append")
    p.add_argument("--clear-blocked-callers", action="store_true")
    p.add_argument("--unknown-callers", choices=["allow", "deny"])
    p.add_argument("--business-hours")
    p.add_argument("--clear-business-hours", action="store_true")
    p.add_argument("--business-days")
    p.add_argument("--auto-reject", choices=["on", "off"])
    p.add_argument("--log-calls", choices=["on", "off"])
    p.add_argument("--clear-bindings", action="store_true")
    p.set_defaults(func=configure_cmd)

    p = sub.add_parser("up", help="create the current audio bridge now")
    p.add_argument("--device")
    p.add_argument("--sink")
    p.add_argument("--source")
    p.add_argument("--echo-cancel", choices=["on", "off"])
    p.add_argument("--latency", type=int)
    p.set_defaults(func=bridge_up)

    p = sub.add_parser("down", help="remove current audio bridge")
    p.set_defaults(func=bridge_down)

    p = sub.add_parser("daemon", help="run the auto-bridge daemon")
    p.add_argument("--device")
    p.add_argument("--sink")
    p.add_argument("--source")
    p.add_argument("--echo-cancel", choices=["on", "off"])
    p.add_argument("--latency", type=int)
    p.add_argument("--auto-answer", choices=["on", "off"])
    p.add_argument("--auto-answer-delay", type=int)
    p.set_defaults(func=daemon_cmd)

    p = sub.add_parser("logs", help="show daemon logs")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=100)
    p.set_defaults(func=logs_cmd)

    p = sub.add_parser("calls", help="list recent call sessions")
    p.add_argument("-n", "--lines", type=int, default=20)
    p.set_defaults(func=calls_cmd)

    p = sub.add_parser("call-show", help="show one recorded call session")
    p.add_argument("session_id")
    p.set_defaults(func=call_show_cmd)

    p = sub.add_parser("dial", help="dial a number over ADB (optional helper)")
    p.add_argument("number")
    p.add_argument("--serial")
    p.set_defaults(func=dial_cmd)

    p = sub.add_parser("hangup", help="hang up the current Android call over ADB")
    p.add_argument("--serial")
    p.set_defaults(func=hangup_cmd)

    p = sub.add_parser("answer", help="answer the current Android call over ADB")
    p.add_argument("--serial")
    p.set_defaults(func=answer_cmd)

    p = sub.add_parser("install-user-config", help="install the WirePlumber config into the current user")
    p.set_defaults(func=install_user_cmd)

    return parser


def main() -> int:
    try:
        parser = build_parser()
        args = parser.parse_args()
        args.func(args)
        return 0
    except CommandError as exc:
        log(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
