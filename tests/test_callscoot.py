import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load_module(name: str, path: Path, temp_home: Path):
    os.environ["XDG_CONFIG_HOME"] = str(temp_home / ".config")
    os.environ["XDG_STATE_HOME"] = str(temp_home / ".state")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CallScootTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        sys.modules.pop("callscoot", None)
        self.callscoot = load_module("callscoot", SRC / "callscoot.py", self.home)

    def tearDown(self):
        self.tempdir.cleanup()
        sys.modules.pop("callscoot", None)

    def test_normalize_phone_number(self):
        self.assertEqual(self.callscoot.normalize_phone_number("+90 531 458 41 41"), "+905314584141")
        self.assertEqual(self.callscoot.normalize_phone_number("0531-458-4141"), "05314584141")
        self.assertIsNone(self.callscoot.normalize_phone_number(""))

    def test_parse_adb_devices(self):
        text = """List of devices attached\n57U7 device product:PRA model:HUAWEI_P9_lite_2017 device:HWPRA-H\nABC unauthorized usb:1-2\n"""
        rows = self.callscoot.parse_adb_devices(text)
        self.assertEqual(rows[0]["serial"], "57U7")
        self.assertEqual(rows[0]["model"], "HUAWEI_P9_lite_2017")
        self.assertEqual(rows[1]["state"], "unauthorized")

    def test_allowlist_policy(self):
        cfg = self.callscoot.DEFAULTS.copy()
        cfg.update({
            "call_policy_mode": "allowlist",
            "allowed_callers": ["+905314584141"],
            "unknown_callers": "deny",
            "auto_reject_blocked": True,
        })
        allowed = self.callscoot.evaluate_call_policy(cfg, {"incoming_number": "+905314584141"})
        denied = self.callscoot.evaluate_call_policy(cfg, {"incoming_number": "+905554443322"})
        self.assertEqual(allowed["action"], "answer")
        self.assertEqual(denied["action"], "reject")

    def test_blocklist_and_hours_policy(self):
        cfg = self.callscoot.DEFAULTS.copy()
        cfg.update({
            "call_policy_mode": "blocklist",
            "blocked_callers": ["*4443322"],
            "auto_reject_blocked": True,
            "business_hours": "09:00-18:00",
            "business_days": ["mon", "tue", "wed", "thu", "fri"],
        })
        blocked = self.callscoot.evaluate_call_policy(cfg, {"incoming_number": "+905554443322"})
        self.assertEqual(blocked["action"], "reject")
        self.assertFalse(self.callscoot.within_business_hours(cfg, datetime(2026, 4, 11, 12, 0)))  # saturday

    def test_call_session_roundtrip(self):
        session = self.callscoot.create_call_session({"state": "ringing", "incoming_number": "+905314584141"})
        self.callscoot.append_call_transcript(session["id"], "caller", "Merhaba")
        self.callscoot.finalize_call_session(session["id"], summary="Done")
        loaded = self.callscoot.read_call_session(session["id"])
        self.assertEqual(loaded["meta"]["summary"], "Done")
        self.assertEqual(loaded["transcript"][0]["text"], "Merhaba")

    def test_public_config_masks_sip_password_and_auto_backend(self):
        cfg = self.callscoot.DEFAULTS.copy()
        cfg.update({
            "telephony_backend": "auto",
            "sip_server": "sip.example.com",
            "sip_username": "1001",
            "sip_password": "secret",
        })
        public = self.callscoot.public_config(cfg)
        self.assertEqual(public["sip_password"], "***")
        self.assertEqual(self.callscoot.selected_telephony_backend(cfg), "sip")


if __name__ == "__main__":
    unittest.main()
