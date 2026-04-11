import importlib.util
import os
import sys
import tempfile
import unittest
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


class CallScootAgentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        sys.modules.pop("callscoot", None)
        sys.modules.pop("callscoot_agent", None)
        self.callscoot = load_module("callscoot", SRC / "callscoot.py", self.home)
        self.agent = load_module("callscoot_agent", SRC / "callscoot_agent.py", self.home)

    def tearDown(self):
        self.tempdir.cleanup()
        sys.modules.pop("callscoot", None)
        sys.modules.pop("callscoot_agent", None)

    def test_mock_reply_pipeline(self):
        cfg = self.agent.load_agent_config()
        cfg["llm_provider"] = "mock"
        cfg["tts_provider"] = "mock"
        result = self.agent.respond_once("Merhaba", cfg)
        self.assertIn("reply", result)
        self.assertGreater(result["audio_bytes"], 0)

    def test_segmenter_emits_after_silence(self):
        seg = self.agent.SpeechSegmenter(sample_rate=16000, threshold=100, min_speech_sec=0.1, silence_sec=0.1)
        speech = (b"\xff\x7f" * 2000)
        silence = (b"\x00\x00" * 2000)
        out = []
        out.extend(seg.feed(speech))
        out.extend(seg.feed(silence))
        self.assertTrue(out)

    def test_detect_active_call_uses_sip_state_when_selected(self):
        cfg = self.callscoot.load_config()
        cfg.update({
            "telephony_backend": "sip",
            "sip_server": "sip.example.com",
            "sip_username": "1001",
        })
        self.callscoot.save_sip_state({"state": "offhook", "remote_number": "+905551112233", "direction": "outbound"})
        info, route = self.agent.detect_active_call(cfg)
        self.assertEqual(info["state"], "offhook")
        self.assertEqual(info["incoming_number"], "+905551112233")
        self.assertEqual(info["direction"], "outbound")
        self.assertEqual(route["mode"], "sip")


if __name__ == "__main__":
    unittest.main()
