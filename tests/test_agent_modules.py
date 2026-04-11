import importlib.util
import json
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


class AgentModuleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        for name in ["callscoot", "agent_state", "agent_events", "agent_memory"]:
            sys.modules.pop(name, None)
        self.callscoot = load_module("callscoot", SRC / "callscoot.py", self.home)
        self.agent_state = load_module("agent_state", SRC / "agent_state.py", self.home)
        self.agent_events = load_module("agent_events", SRC / "agent_events.py", self.home)
        self.agent_memory = load_module("agent_memory", SRC / "agent_memory.py", self.home)

    def tearDown(self):
        self.tempdir.cleanup()
        for name in ["callscoot", "agent_state", "agent_events", "agent_memory"]:
            sys.modules.pop(name, None)

    def test_state_machine_tracks_turns(self):
        sm = self.agent_state.AgentStateMachine()
        self.assertEqual(sm.state, self.agent_state.AgentState.IDLE)
        sm.set_state(self.agent_state.AgentState.SPEAKING, reason="test")
        self.assertEqual(sm.turn_count, 1)
        sm.set_state(self.agent_state.AgentState.LISTENING, reason="done")
        self.assertEqual(sm.state, self.agent_state.AgentState.LISTENING)

    def test_memory_store_roundtrip(self):
        store = self.agent_memory.MemoryStore()
        store.upsert_profile("+905551112233", name="Efe", tier="gold", notes="VIP")
        store.add_memory("+905551112233", "Prefers WhatsApp")
        profile = store.get_profile("+905551112233")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.name, "Efe")
        memories = store.retrieve_memories("+905551112233")
        self.assertTrue(memories)
        self.assertEqual(memories[0]["memory"], "Prefers WhatsApp")

    def test_event_logger_writes_schema(self):
        session = self.callscoot.create_call_session({"incoming_number": "+905551112233", "state": "offhook"})
        sm = self.agent_state.AgentStateMachine()
        logger = self.agent_events.EventLogger(session["id"], sm, caller_id="+905551112233")
        event = logger.emit("call_started", payload={"foo": "bar"})
        self.assertEqual(event["event_type"], "call_started")
        path = self.callscoot.call_session_dir(session["id"]) / self.agent_events.EVENT_LOG_NAME
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines[0]["payload"]["foo"], "bar")


if __name__ == "__main__":
    unittest.main()
