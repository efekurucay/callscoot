import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CallScootClientTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("callscoot_client", None)
        self.client_mod = load_module("callscoot_client", SRC / "callscoot_client.py")

    def tearDown(self):
        sys.modules.pop("callscoot_client", None)

    def test_iter_sse_events_skips_comments(self):
        events = list(
            self.client_mod.iter_sse_events(
                [
                    ": heartbeat\n",
                    'data: {"type": "call_started"}\n',
                    "\n",
                    'data: {"type": "call_ended"}\n',
                    "\n",
                ]
            )
        )
        self.assertEqual([item["type"] for item in events], ["call_started", "call_ended"])

    def test_wait_for_session_start(self):
        client = self.client_mod.CallScootClient(base_url="http://127.0.0.1:8788")
        states = [None, None, {"id": "session-1"}]

        def fake_current_call():
            return states.pop(0)

        client.current_call = fake_current_call  # type: ignore[method-assign]
        session_id = client.wait_for_session_start(timeout_sec=1.0, poll_interval_sec=0.0)
        self.assertEqual(session_id, "session-1")

    def test_wait_for_session_end(self):
        client = self.client_mod.CallScootClient(base_url="http://127.0.0.1:8788")
        states = [{"id": "session-1"}, {"id": "session-1"}, None]
        session = {"meta": {"id": "session-1", "summary": "Done"}}

        def fake_current_call():
            return states.pop(0)

        def fake_get_call(session_id: str):
            self.assertEqual(session_id, "session-1")
            return session

        client.current_call = fake_current_call  # type: ignore[method-assign]
        client.get_call = fake_get_call  # type: ignore[method-assign]
        result = client.wait_for_session_end("session-1", timeout_sec=1.0, poll_interval_sec=0.0)
        self.assertEqual(result["meta"]["summary"], "Done")


if __name__ == "__main__":
    unittest.main()
