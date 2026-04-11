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


class CallScootApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        for name in ["callscoot", "agent_control", "callscoot_api"]:
            sys.modules.pop(name, None)
        self.callscoot = load_module("callscoot", SRC / "callscoot.py", self.home)
        self.agent_control = load_module("agent_control", SRC / "agent_control.py", self.home)
        self.api = load_module("callscoot_api", SRC / "callscoot_api.py", self.home)

    def tearDown(self):
        self.tempdir.cleanup()
        for name in ["callscoot", "agent_control", "callscoot_api"]:
            sys.modules.pop(name, None)

    def test_normalize_patch_config(self):
        patch = self.api.normalize_patch_config(
            {
                "auto_answer": True,
                "auto_answer_delay_sec": "3",
                "max_call_duration_sec": "600",
                "echo_cancel": False,
                "target_device": "aa:bb:cc:dd:ee:ff",
                "ignored_key": "x",
            }
        )
        self.assertEqual(patch["auto_answer"], True)
        self.assertEqual(patch["auto_answer_delay_sec"], 3)
        self.assertEqual(patch["max_call_duration_sec"], 600)
        self.assertEqual(patch["echo_cancel"], False)
        self.assertEqual(patch["target_device"], "AA:BB:CC:DD:EE:FF")
        self.assertNotIn("ignored_key", patch)

    def test_current_or_session_id(self):
        session = self.callscoot.create_call_session({"incoming_number": "+905551112233", "state": "offhook"})
        self.assertEqual(self.api.current_or_session_id("current"), session["id"])
        self.assertEqual(self.api.current_or_session_id(session["id"]), session["id"])


if __name__ == "__main__":
    unittest.main()
