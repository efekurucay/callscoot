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


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        for name in ["callscoot", "agent_control"]:
            sys.modules.pop(name, None)
        self.callscoot = load_module("callscoot", SRC / "callscoot.py", self.home)
        self.agent_control = load_module("agent_control", SRC / "agent_control.py", self.home)

    def tearDown(self):
        self.tempdir.cleanup()
        for name in ["callscoot", "agent_control"]:
            sys.modules.pop(name, None)

    def test_pending_call_request_claim(self):
        request = self.agent_control.add_pending_call_request(
            "+905551112233",
            dynamic_variables={"campaign_name": "survey"},
            metadata={"lead_id": "1"},
            ttl_sec=60,
        )
        claimed = self.agent_control.claim_pending_call_request(
            {"incoming_number": "+90 555 111 22 33"},
            "session-1",
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["request_id"], request["request_id"])
        self.assertEqual(claimed["dynamic_variables"]["campaign_name"], "survey")

    def test_session_command_queue(self):
        command = self.agent_control.queue_session_command("session-1", "contextual_update", {"text": "hello"})
        items = self.agent_control.pop_session_commands("session-1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["command_id"], command["command_id"])
        self.assertEqual(items[0]["payload"]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
