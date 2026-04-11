from __future__ import annotations

import logging
import re
import threading
from typing import Any

import callscoot

try:
    import pjsua2 as pj

    PJSUA2_AVAILABLE = True
except Exception:  # noqa: BLE001
    pj = None  # type: ignore[assignment]
    PJSUA2_AVAILABLE = False


logger = logging.getLogger(__name__)

SIP_CALLING_STATES = {"calling", "connecting"}
SIP_RINGING_STATES = {"incoming", "early", "ringing"}
SIP_CONNECTED_STATES = {"confirmed"}
SIP_IDLE_STATES = {"disconnected", "disconnctd", "null", "idle"}

SIP_AUDIO_MODE_DIRECT = "direct"
SIP_AUDIO_MODE_AGENT = "agent"
AGENT_RX_SINK_NAME = "callscoot.agent.rx"
AGENT_TX_SINK_NAME = "callscoot.agent.tx"
AGENT_TX_MONITOR_SOURCE = f"{AGENT_TX_SINK_NAME}.monitor"


def _normalize_remote_number(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"sip:([^>;]+)", value, re.IGNORECASE)
    token = match.group(1) if match else value
    token = token.split("@", 1)[0].split(";", 1)[0].strip()
    return callscoot.normalize_phone_number(token) or token or None


if PJSUA2_AVAILABLE:

    class ManagedCall(pj.Call):
        def __init__(self, owner: "SIPTelephonyBackend", account: pj.Account, call_id: int = pj.PJSUA_INVALID_ID):
            super().__init__(account, call_id)
            self.owner = owner

        def onCallState(self, prm: pj.OnCallStateParam) -> None:  # noqa: N802
            self.owner.handle_call_state(self)

        def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:  # noqa: N802
            self.owner.handle_call_media_state(self)

    class ManagedAccount(pj.Account):
        def __init__(self, owner: "SIPTelephonyBackend"):
            super().__init__()
            self.owner = owner

        def onRegState(self, prm: pj.OnRegStateParam) -> None:  # noqa: N802
            self.owner.handle_registration_state()

        def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:  # noqa: N802
            call = ManagedCall(self.owner, self, prm.callId)
            self.owner.attach_call(call, direction="inbound")
            self.owner.handle_call_state(call)

else:

    class ManagedCall:  # type: ignore[no-redef]
        pass

    class ManagedAccount:  # type: ignore[no-redef]
        pass


class SIPTelephonyBackend:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg.copy()
        self._lock = threading.RLock()
        self._ep: Any = None
        self._acc: Any = None
        self._call: Any = None
        self._started = False
        self._registered = False
        self._registration_status_code = 0
        self._registration_status_text = ""
        self._state = "idle"
        self._direction: str | None = None
        self._session_id: str | None = None
        self._remote_uri: str | None = None
        self._last_status_code = 0
        self._last_error: str | None = None
        self._selected_capture_device: str | None = None
        self._selected_playback_device: str | None = None
        self._audio_mode = str(self.cfg.get("sip_audio_mode") or SIP_AUDIO_MODE_DIRECT).lower()
        if self._audio_mode not in {SIP_AUDIO_MODE_DIRECT, SIP_AUDIO_MODE_AGENT}:
            self._audio_mode = SIP_AUDIO_MODE_DIRECT
        self._routed_sink_input_id: int | None = None
        self._routed_source_output_id: int | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                self._write_runtime_state()
                return
            if not PJSUA2_AVAILABLE:
                self._last_error = "pjsua2 is not installed. Install pjproject/pjsua2 to enable SIP mode."
                self._write_runtime_state()
                raise RuntimeError(self._last_error)
            try:
                self._ep = pj.Endpoint()
                self._ep.libCreate()
                ep_cfg = pj.EpConfig()
                ep_cfg.logConfig.level = 1
                ep_cfg.logConfig.consoleLevel = 1
                ep_cfg.medConfig.noVad = True
                self._ep.libInit(ep_cfg)
                transport_cfg = pj.TransportConfig()
                transport_cfg.port = 0
                transport = str(self.cfg.get("sip_transport") or "udp").lower()
                if transport == "tcp":
                    self._ep.transportCreate(pj.PJSIP_TRANSPORT_TCP, transport_cfg)
                elif transport == "tls":
                    self._ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, transport_cfg)
                else:
                    self._ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport_cfg)
                self._configure_audio_devices()
                self._ep.libStart()
                self._acc = ManagedAccount(self)
                self._acc.create(self._build_account_config())
                self._started = True
                self._registered = False
                self._state = "idle"
                self._last_error = None
                self._finalize_stale_session_if_needed()
                self._write_runtime_state()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self._write_runtime_state()
                self._destroy_endpoint()
                raise

    def stop(self) -> None:
        with self._lock:
            if self._call is not None:
                try:
                    prm = pj.CallOpParam() if PJSUA2_AVAILABLE else None
                    self._call.hangup(prm)
                except Exception:  # noqa: BLE001
                    pass
            self._call = None
            self._state = "idle"
            self._direction = None
            self._remote_uri = None
            self._last_status_code = 0
            self._clear_agent_audio_routing()
            self._destroy_endpoint()
            self._registered = False
            self._registration_status_code = 0
            self._registration_status_text = ""
            self._started = False
            self._write_runtime_state()

    def dial(self, number: str) -> dict[str, Any]:
        target = str(number or "").strip()
        if not target:
            raise RuntimeError("number is required")
        self.start()
        with self._lock:
            if self._call is not None and self._state not in {"idle"}:
                raise RuntimeError(f"cannot dial while SIP call state={self._state}")
            if "@" not in target:
                uri = f"sip:{target}@{self.cfg['sip_server']}"
            elif target.lower().startswith("sip:"):
                uri = target
            else:
                uri = f"sip:{target}"
            if self._acc is None:
                raise RuntimeError("SIP account is not ready")
            self._call = ManagedCall(self, self._acc)
            self._direction = "outbound"
            self._remote_uri = uri
            self._state = "calling"
            self._last_status_code = 0
            self._last_error = None
            self._ensure_session(state="calling", remote_uri=uri)
            self._append_state_event("sip_call_state_changed", previous_state="idle", state="calling", remote_uri=uri)
            prm = pj.CallOpParam(True)
            self._call.makeCall(uri, prm)
            self._write_runtime_state()
            return {"via": "sip", "backend": "sip", "uri": uri, "session_id": self._session_id}

    def answer(self) -> dict[str, Any]:
        self.start()
        with self._lock:
            if self._call is None:
                raise RuntimeError("no active SIP call")
            prm = pj.CallOpParam()
            prm.statusCode = 200
            self._call.answer(prm)
            return {"via": "sip", "backend": "sip", "answering": True, "session_id": self._session_id}

    def hangup(self) -> dict[str, Any]:
        self.start()
        with self._lock:
            if self._call is None and self._state == "idle":
                raise RuntimeError("no active SIP call")
            if self._call is not None:
                prm = pj.CallOpParam()
                self._call.hangup(prm)
            return {"via": "sip", "backend": "sip", "queued": True, "session_id": self._session_id}

    def attach_call(self, call: ManagedCall, direction: str) -> None:
        with self._lock:
            self._call = call
            self._direction = direction
            self._last_error = None

    def handle_registration_state(self) -> None:
        with self._lock:
            if self._acc is None:
                return
            try:
                info = self._acc.getInfo()
                self._registered = bool(getattr(info, "regIsActive", False))
                self._registration_status_code = int(getattr(info, "regStatus", 0) or 0)
                self._registration_status_text = str(getattr(info, "regStatusText", "") or "")
                self._last_error = None if self._registration_status_code in {0, 200} else self._registration_status_text or self._last_error
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
            self._write_runtime_state()

    def handle_call_media_state(self, call: ManagedCall) -> None:
        with self._lock:
            if not PJSUA2_AVAILABLE or self._ep is None:
                self._write_runtime_state()
                return
            try:
                info = call.getInfo()
                for media in getattr(info, "media", []):
                    if getattr(media, "type", None) != pj.PJMEDIA_TYPE_AUDIO:
                        continue
                    if getattr(media, "status", None) != pj.PJSUA_CALL_MEDIA_ACTIVE:
                        continue
                    call_media = call.getAudioMedia(getattr(media, "index", 0))
                    adm = self._ep.audDevManager()
                    capture = adm.getCaptureDevMedia()
                    playback = adm.getPlaybackDevMedia()
                    capture.startTransmit(call_media)
                    call_media.startTransmit(playback)
                    break
                self._last_error = None
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
            self._ensure_audio_mode_routing()
            self._write_runtime_state()

    def handle_call_state(self, call: ManagedCall) -> None:
        with self._lock:
            try:
                info = call.getInfo()
                state_text = str(getattr(info, "stateText", "") or "").strip().lower()
                last_status = int(getattr(info, "lastStatusCode", 0) or 0)
                remote_uri = str(getattr(info, "remoteUri", "") or self._remote_uri or "")
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self._write_runtime_state()
                return
            previous_state = self._state
            mapped = self._map_call_state(state_text)
            self._state = mapped
            self._last_status_code = last_status
            self._remote_uri = remote_uri or self._remote_uri
            self._last_error = None if mapped != "idle" or last_status in {0, 200, 486, 487, 603} else self._last_error
            if mapped in {"calling", "ringing", "offhook"}:
                self._ensure_session(state=mapped, remote_uri=self._remote_uri)
            self._append_state_event(
                "sip_call_state_changed",
                previous_state=previous_state,
                state=mapped,
                status_code=last_status,
                remote_uri=self._remote_uri,
            )
            if self._session_id:
                callscoot.update_call_session(
                    self._session_id,
                    state=mapped,
                    incoming_number=_normalize_remote_number(self._remote_uri),
                    direction=self._direction,
                    adb_serial=None,
                    target_mac=None,
                    target_name=self.cfg.get("sip_server"),
                    telephony_backend="sip",
                )
                if mapped == "offhook":
                    current = callscoot.load_current_call() or {}
                    if not current.get("offhook_started_at"):
                        callscoot.update_call_session(self._session_id, offhook_started_at=callscoot.utc_now_iso())
            if mapped == "idle":
                self._clear_agent_audio_routing()
                self._finish_session()
                self._call = None
                self._direction = None
                self._remote_uri = None
            else:
                self._ensure_audio_mode_routing()
            self._write_runtime_state()

    def _finish_session(self) -> None:
        if not self._session_id:
            current = callscoot.load_current_call()
            if current and current.get("telephony_backend") == "sip":
                self._session_id = current.get("id")
        if self._session_id:
            callscoot.finalize_call_session(
                self._session_id,
                incoming_number=_normalize_remote_number(self._remote_uri),
                direction=self._direction,
                adb_serial=None,
                target_mac=None,
                target_name=self.cfg.get("sip_server"),
                telephony_backend="sip",
            )
        self._session_id = None

    def _ensure_session(self, state: str, remote_uri: str | None) -> None:
        number = _normalize_remote_number(remote_uri)
        call_info = {
            "state": state,
            "incoming_number": number,
            "direction": self._direction,
            "adb_serial": None,
            "target_mac": None,
            "target_name": self.cfg.get("sip_server"),
        }
        if not self._session_id:
            session = callscoot.create_call_session(
                call_info,
                extra={
                    "policy": callscoot.caller_lists_snapshot(self.cfg),
                    "telephony_backend": "sip",
                },
            )
            self._session_id = str(session["id"])
        else:
            callscoot.update_call_session(
                self._session_id,
                state=state,
                incoming_number=number,
                direction=self._direction,
                adb_serial=None,
                target_mac=None,
                target_name=self.cfg.get("sip_server"),
                telephony_backend="sip",
            )

    def _append_state_event(self, event_type: str, **payload: Any) -> None:
        if self._session_id:
            callscoot.append_call_event(self._session_id, event_type, **payload)

    def _finalize_stale_session_if_needed(self) -> None:
        current = callscoot.load_current_call()
        if current and current.get("telephony_backend") == "sip" and current.get("state") not in {"idle", None}:
            callscoot.finalize_call_session(
                str(current.get("id")),
                incoming_number=current.get("incoming_number"),
                direction=current.get("direction"),
                adb_serial=None,
                target_mac=None,
                target_name=self.cfg.get("sip_server"),
                telephony_backend="sip",
            )

    def _map_call_state(self, state_text: str) -> str:
        if state_text in SIP_CALLING_STATES:
            return "calling"
        if state_text in SIP_RINGING_STATES:
            return "ringing"
        if state_text in SIP_CONNECTED_STATES:
            return "offhook"
        if state_text in SIP_IDLE_STATES:
            return "idle"
        return self._state or "idle"

    def _build_account_config(self) -> Any:
        account_cfg = pj.AccountConfig()
        server = str(self.cfg.get("sip_server") or "").strip()
        username = str(self.cfg.get("sip_username") or "").strip()
        password = str(self.cfg.get("sip_password") or "")
        port = int(self.cfg.get("sip_port") or 5060)
        account_cfg.idUri = f"sip:{username}@{server}"
        account_cfg.regConfig.registrarUri = f"sip:{server}:{port}"
        account_cfg.regConfig.registerOnAdd = True
        account_cfg.regConfig.timeoutSec = 300
        if username:
            account_cfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", username, 0, password))
        return account_cfg

    def _configure_audio_devices(self) -> None:
        if not self._ep:
            return
        self._audio_mode = str(self.cfg.get("sip_audio_mode") or SIP_AUDIO_MODE_DIRECT).lower()
        if self._audio_mode not in {SIP_AUDIO_MODE_DIRECT, SIP_AUDIO_MODE_AGENT}:
            self._audio_mode = SIP_AUDIO_MODE_DIRECT
        if self._audio_mode == SIP_AUDIO_MODE_AGENT:
            wanted_capture = str(self.cfg.get("sip_capture_device") or "pipewire").strip() or "pipewire"
            wanted_playback = str(self.cfg.get("sip_playback_device") or "pipewire").strip() or "pipewire"
        else:
            wanted_capture = str(self.cfg.get("sip_capture_device") or self.cfg.get("local_source") or "").strip() or None
            wanted_playback = str(self.cfg.get("sip_playback_device") or self.cfg.get("local_sink") or "").strip() or None
        adm = self._ep.audDevManager()
        devices = self._enumerate_audio_devices(adm)
        capture = self._match_audio_device(devices, wanted_capture, want_input=True)
        playback = self._match_audio_device(devices, wanted_playback, want_output=True)
        if capture is not None:
            adm.setCaptureDev(capture["index"])
            self._selected_capture_device = str(capture["name"])
        else:
            self._selected_capture_device = wanted_capture or "system-default"
        if playback is not None:
            adm.setPlaybackDev(playback["index"])
            self._selected_playback_device = str(playback["name"])
        else:
            self._selected_playback_device = wanted_playback or "system-default"

    def _enumerate_audio_devices(self, adm: Any) -> list[dict[str, Any]]:
        method = None
        for candidate in ("enumDev2", "enumDev"):
            if hasattr(adm, candidate):
                method = getattr(adm, candidate)
                break
        if method is None:
            return []
        rows: list[dict[str, Any]] = []
        for index, dev in enumerate(method()):
            rows.append(
                {
                    "index": index,
                    "name": str(getattr(dev, "name", "") or ""),
                    "driver": str(getattr(dev, "driver", "") or ""),
                    "input_count": int(getattr(dev, "inputCount", 0) or 0),
                    "output_count": int(getattr(dev, "outputCount", 0) or 0),
                }
            )
        return rows

    def _match_audio_device(self, devices: list[dict[str, Any]], wanted: str | None, want_input: bool = False, want_output: bool = False) -> dict[str, Any] | None:
        if not wanted:
            return None
        needle = wanted.strip().lower()
        exact: dict[str, Any] | None = None
        partial: dict[str, Any] | None = None
        for dev in devices:
            if want_input and dev.get("input_count", 0) <= 0:
                continue
            if want_output and dev.get("output_count", 0) <= 0:
                continue
            haystacks = [str(dev.get("name") or "").lower(), f"{dev.get('driver') or ''}:{dev.get('name') or ''}".lower()]
            if needle in haystacks:
                exact = dev
                break
            if any(needle in item for item in haystacks):
                partial = partial or dev
        return exact or partial

    def _ensure_audio_mode_routing(self) -> None:
        if self._audio_mode != SIP_AUDIO_MODE_AGENT:
            return
        if self._state not in {"calling", "ringing", "offhook"}:
            return
        try:
            self._ensure_agent_sinks()
            sink_input_id, source_output_id = self._find_sip_audio_streams()
            if sink_input_id is None or source_output_id is None:
                return
            callscoot.move_sink_input(sink_input_id, AGENT_RX_SINK_NAME)
            callscoot.move_source_output(source_output_id, AGENT_TX_MONITOR_SOURCE)
            self._routed_sink_input_id = sink_input_id
            self._routed_source_output_id = source_output_id
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)

    def _clear_agent_audio_routing(self) -> None:
        self._routed_sink_input_id = None
        self._routed_source_output_id = None

    def _ensure_agent_sinks(self) -> None:
        if not callscoot.sink_exists(AGENT_RX_SINK_NAME):
            callscoot.pulse_module_load(
                "module-null-sink",
                sink_name=AGENT_RX_SINK_NAME,
                sink_properties="device.description=CallScoot-AI-RX",
                rate=16000,
                channels=1,
                channel_map="mono",
            )
            callscoot.wait_for_sink(AGENT_RX_SINK_NAME, timeout=5.0)
        if not callscoot.sink_exists(AGENT_TX_SINK_NAME):
            callscoot.pulse_module_load(
                "module-null-sink",
                sink_name=AGENT_TX_SINK_NAME,
                sink_properties="device.description=CallScoot-AI-TX",
                rate=16000,
                channels=1,
                channel_map="mono",
            )
            callscoot.wait_for_sink(AGENT_TX_SINK_NAME, timeout=5.0)
        if not callscoot.source_exists(AGENT_TX_MONITOR_SOURCE):
            callscoot.wait_for_source(AGENT_TX_MONITOR_SOURCE, timeout=5.0)

    def _find_sip_audio_streams(self) -> tuple[int | None, int | None]:
        sink_input_id: int | None = None
        source_output_id: int | None = None
        for item in callscoot.list_sink_inputs():
            props = item.get("properties") or {}
            app_name = str(props.get("application.name") or "")
            media_name = str(props.get("media.name") or "")
            node_name = str(props.get("node.name") or "")
            if "PipeWire ALSA [python" not in app_name:
                continue
            if media_name != "ALSA Playback" and not node_name.startswith("alsa_playback.python"):
                continue
            sink_input_id = int(item.get("index"))
            break
        for item in callscoot.list_source_outputs():
            props = item.get("properties") or {}
            app_name = str(props.get("application.name") or "")
            media_name = str(props.get("media.name") or "")
            node_name = str(props.get("node.name") or "")
            if "PipeWire ALSA [python" not in app_name:
                continue
            if media_name != "ALSA Capture" and not node_name.startswith("alsa_capture.python"):
                continue
            source_output_id = int(item.get("index"))
            break
        return sink_input_id, source_output_id

    def _destroy_endpoint(self) -> None:
        if self._ep is None:
            return
        try:
            self._ep.libDestroy()
        except Exception:  # noqa: BLE001
            pass
        self._acc = None
        self._ep = None

    def _write_runtime_state(self) -> None:
        callscoot.save_sip_state(
            {
                "configured": callscoot.sip_configured(self.cfg),
                "available": PJSUA2_AVAILABLE,
                "started": self._started,
                "registered": self._registered,
                "registration_status_code": self._registration_status_code,
                "registration_status_text": self._registration_status_text,
                "state": self._state,
                "direction": self._direction,
                "remote_uri": self._remote_uri,
                "remote_number": _normalize_remote_number(self._remote_uri),
                "session_id": self._session_id,
                "server": self.cfg.get("sip_server"),
                "transport": self.cfg.get("sip_transport"),
                "port": int(self.cfg.get("sip_port") or 5060),
                "audio_mode": self._audio_mode,
                "capture_device": self._selected_capture_device,
                "playback_device": self._selected_playback_device,
                "routed_sink_input_id": self._routed_sink_input_id,
                "routed_source_output_id": self._routed_source_output_id,
                "last_status_code": self._last_status_code,
                "last_error": self._last_error,
            }
        )
