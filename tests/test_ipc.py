"""File IPC between mcp_server.bridge.client and renderdoc_extension.socket_server, without RenderDoc.

The server runs in its worker-thread mode (no PySide2 here) with a UI-invoke stand-in that calls
directly, and an echo handler.
"""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _EchoHandler:
    def __init__(self):
        self.calls = []
        self.requests = []

    def handle(self, request):
        self.calls.append(request["method"])
        self.requests.append(request)
        if request["method"] == "slow":
            time.sleep(0.3)
        if request["method"] == "fail":
            return {"id": request["id"], "error": {"code": -32000, "message": "boom"}}
        return {"id": request["id"], "result": {"method": request["method"], "params": request["params"]}}


class IpcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"RENDERDOC_MCP_IPC_DIR": cls.tmp.name})
        cls.env.start()
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        # module-level IPC paths are read at import time, so both modules are loaded with the temp directory
        # set. socket_server is loaded on its own: the package __init__ needs RenderDoc's module.
        spec = importlib.util.spec_from_file_location(
            "_ipc_socket_server", os.path.join(ROOT, "renderdoc_extension", "socket_server.py"))
        cls.server_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server_mod)
        import mcp_server.bridge.client as client_mod
        cls.client_mod = importlib.reload(client_mod)
        cls.handler = _EchoHandler()
        cls.server = cls.server_mod.MCPBridgeServer("127.0.0.1", 0, cls.handler, ui_invoke=lambda fn: fn())
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.env.stop()
        import mcp_server.bridge.client as client_mod
        importlib.reload(client_mod)
        cls.tmp.cleanup()

    def client(self):
        return self.client_mod.RenderDocBridge()

    def test_round_trip_uses_per_request_files_and_cleans_up(self):
        self.assertEqual(self.client().call("ping", {"x": 1}, timeout=5), {"method": "ping", "params": {"x": 1}})
        time.sleep(0.05)
        leftovers = [n for n in os.listdir(self.tmp.name) if n.startswith(("request", "response"))]
        self.assertEqual(leftovers, [])

    def test_error_response_raises(self):
        with self.assertRaises(self.client_mod.RenderDocBridgeError) as cm:
            self.client().call("fail", timeout=5)
        self.assertIn("boom", str(cm.exception))

    def test_concurrent_clients_each_get_their_own_answer(self):
        results, errors = {}, []

        def worker(n):
            try:
                results[n] = self.client().call("slow" if n % 2 else "ping", {"n": n}, timeout=10)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(6)))
        for n, r in results.items():
            self.assertEqual(r["params"], {"n": n})

    def test_legacy_single_slot_protocol_is_still_served(self):
        d = self.tmp.name
        with open(os.path.join(d, "lock"), "w") as f:
            f.write("lock")
        with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "old", "method": "ping", "params": {}}, f)
        os.remove(os.path.join(d, "lock"))
        deadline = time.time() + 5
        while not os.path.exists(os.path.join(d, "response.json")) and time.time() < deadline:
            time.sleep(0.05)
        with open(os.path.join(d, "response.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["id"], "old")
        os.remove(os.path.join(d, "response.json"))

    def _drop_request(self, rid, body):
        path = os.path.join(self.tmp.name, "request_%s.json" % rid)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(dict(body, id=rid), f)
        os.replace(path + ".tmp", path)
        return path, os.path.join(self.tmp.name, "response_%s.json" % rid)

    def _wait_gone(self, path, seconds=5):
        deadline = time.time() + seconds
        while os.path.exists(path) and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)

    def test_client_sends_the_time_it_stops_waiting(self):
        before = time.time()
        self.client().call("ping", timeout=7)
        sent = [r for r in self.handler.requests if r["method"] == "ping"][-1]["deadline"]
        self.assertTrue(before + 7 <= sent <= time.time() + 7)

    def test_request_whose_client_is_gone_is_dropped_unanswered(self):
        before = len(self.handler.calls)
        req, resp = self._drop_request("stale", {"method": "stale_probe", "params": {}, "deadline": time.time() - 60})
        self._wait_gone(req)
        self.assertNotIn("stale_probe", self.handler.calls[before:])
        self.assertFalse(os.path.exists(req))                       # picked up and discarded, not left to retry
        self.assertFalse(os.path.exists(resp))                      # nobody is waiting: no orphan response file

    def test_deadline_has_a_clock_slack(self):
        req, resp = self._drop_request("slack", {"method": "slack_probe", "params": {}, "deadline": time.time() - 0.5})
        deadline = time.time() + 5
        while not os.path.exists(resp) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(resp))
        os.remove(resp)

    def test_requests_without_a_deadline_are_still_served(self):
        """clients that predate the deadline field"""
        req, resp = self._drop_request("old", {"method": "old_probe", "params": {}})
        deadline = time.time() + 5
        while not os.path.exists(resp) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(resp))
        with open(resp, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["result"]["method"], "old_probe")
        os.remove(resp)

    def test_timed_out_request_is_withdrawn(self):
        self.server._running = False                  # stop serving for a moment
        try:
            with self.assertRaises(self.client_mod.RenderDocBridgeError):
                self.client().call("ping", timeout=0.3)
            pending = [n for n in os.listdir(self.tmp.name) if n.startswith("request_")]
            self.assertEqual(pending, [])
        finally:
            self.server._running = True
            self.server._thread = threading.Thread(target=self.server._poll_loop, daemon=True)
            self.server._thread.start()

    def test_response_briefly_locked_by_another_process_is_retried(self):
        real_open = open
        state = {"denied": 0}

        def flaky_open(path, *args, **kwargs):
            name = os.path.basename(str(path))
            reading = not args or "r" in args[0]
            if name.startswith("response_") and name.endswith(".json") and reading and state["denied"] < 3:
                state["denied"] += 1
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", flaky_open):
            self.assertEqual(self.client().call("ping", timeout=5)["method"], "ping")
        self.assertEqual(state["denied"], 3)


if __name__ == "__main__":
    unittest.main()
