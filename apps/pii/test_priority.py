"""Local-socket priority ordering and background deadline isolation."""

import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.pii.shared_client import SharedPiiError, SharedPiiPipeline
from apps.pii.test_shared_detector import _frame, _read_frame, _running_server, _scripted_server


class PriorityTests(SimpleTestCase):
    def test_foreground_overtakes_background_and_each_class_is_fifo(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def detect(text):
            seen.append(text)
            if text == "running":
                entered.set()
                release.wait(3)
            return []

        connections = []
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with _running_server(path, detect) as server:
                try:
                    for name, priority in [
                        ("running", "background"),
                        ("bg1", "background"),
                        ("bg2", "background"),
                        ("fg1", "foreground"),
                        ("legacy-fg2", None),
                    ]:
                        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        connection.settimeout(4)
                        connection.connect(path)
                        connections.append(connection)
                        payload = {"v": 1, "engine": "deberta", "text": name, "ttl_ms": 4000}
                        if priority is not None:
                            payload["priority"] = priority
                        connection.sendall(_frame(payload))
                        if name == "running":
                            self.assertTrue(entered.wait(1))
                        else:
                            until = time.monotonic() + 1
                            while server._jobs.qsize() < len(connections) - 1 and time.monotonic() < until:
                                time.sleep(0.002)
                            self.assertEqual(server._jobs.qsize(), len(connections) - 1)
                    self.assertEqual(seen, ["running"])
                    release.set()
                    for connection in connections:
                        self.assertEqual(_read_frame(connection)["spans"], [])
                    self.assertEqual(seen, ["running", "fg1", "legacy-fg2", "bg1", "bg2"])
                finally:
                    release.set()
                    for connection in connections:
                        connection.close()

    def test_background_has_30_seconds_foreground_keeps_5(self):
        response = {"v": 1, "engine": "liquid", "spans": []}
        for background, limit in [(False, 5), (True, 30)]:
            with self.subTest(background=background), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                with patch.dict(os.environ, PII_BACKGROUND_DEADLINE_S="30"), _scripted_server(path, response) as seen:
                    client = SharedPiiPipeline(engine="liquid", socket_path=path, deadline_s=5)
                    client("synthetic", background=background, deadline=time.monotonic() + 60)
                self.assertGreater(seen[0]["ttl_ms"], (limit - 1) * 1000)
                self.assertLessEqual(seen[0]["ttl_ms"], limit * 1000)
                self.assertEqual(seen[0].get("priority", "foreground"), "background" if background else "foreground")

    def test_background_still_obeys_shorter_caller_deadline(self):
        response = {"v": 1, "engine": "liquid", "spans": []}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with _scripted_server(path, response) as seen:
                SharedPiiPipeline(engine="liquid", socket_path=path)(
                    "synthetic", background=True, deadline=time.monotonic() + 2
                )
            self.assertLessEqual(seen[0]["ttl_ms"], 2000)

    def test_background_timeouts_neither_open_nor_reset_foreground_breaker(self):
        client = SharedPiiPipeline(engine="liquid")
        client._consecutive_failures = 2
        with patch.object(client, "_call", side_effect=SharedPiiError("synthetic", outcome="timeout")):
            for _ in range(5):
                with self.assertRaises(SharedPiiError):
                    client("synthetic", background=True)
        self.assertEqual(client._consecutive_failures, 2)
        self.assertEqual(client._open_until, 0)
        with patch.object(client, "_call", return_value=[]):
            client("synthetic", background=True)
        self.assertEqual(client._consecutive_failures, 2)
        client._open_until = time.monotonic() + 60
        with patch.object(client, "_call", return_value=[]):
            self.assertEqual(client("synthetic", background=True), [])
            with self.assertRaises(SharedPiiError) as caught:
                client("synthetic")
        self.assertEqual(caught.exception.outcome, "circuit_open")

    def test_unknown_priority_is_rejected_and_capacity_is_shared(self):
        from queue import Full
        from types import SimpleNamespace

        from apps.pii.shared_server import SharedDetectorServer, _PriorityJobs

        server = SharedDetectorServer(engine="liquid", configure_runtime=False)
        request = {"v": 1, "engine": "liquid", "text": "synthetic", "ttl_ms": 1000}
        for priority in (None, "urgent", 1, []):
            self.assertEqual(server._validate_request({**request, "priority": priority}), "bad_request")
        jobs = _PriorityJobs(maxsize=2)
        jobs.put_nowait(SimpleNamespace(background=True))
        jobs.put_nowait(SimpleNamespace(background=False))
        with self.assertRaises(Full):
            jobs.put_nowait(SimpleNamespace(background=False))
        self.assertFalse(jobs.get_nowait().background)
        self.assertTrue(jobs.get_nowait().background)

    def test_actual_background_socket_timeout_leaves_foreground_breaker_unchanged(self):
        response = {"v": 1, "engine": "liquid", "spans": []}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            client = SharedPiiPipeline(engine="liquid", socket_path=path)
            with _scripted_server(path, response, delay=0.1), self.assertRaises(SharedPiiError) as caught:
                client("synthetic", background=True, deadline=time.monotonic() + 0.02)
            self.assertEqual(caught.exception.outcome, "timeout")
            self.assertEqual(client._consecutive_failures, 0)
            self.assertEqual(client._open_until, 0)
            with _scripted_server(path, response):
                self.assertEqual(client("synthetic"), [])

    def test_server_passes_request_deadline_to_model_owned_windows(self):
        from apps.pii.liquid_engine import _LiquidPiiPipeline

        deadlines = []

        class Pipeline(_LiquidPiiPipeline):
            def __call__(self, text, *, deadline=None, on_window_deadline=None):
                deadlines.append(deadline)
                return []

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "pii.sock")
            with _running_server(path, Pipeline(None, None, None), engine="liquid"):
                now = time.monotonic()
                SharedPiiPipeline(engine="liquid", socket_path=path)("synthetic", background=True)
                self.assertGreater(deadlines[0], now + 29)
                self.assertLessEqual(deadlines[0], time.monotonic() + 29.75)

    def test_server_logs_one_count_only_line_when_window_budget_is_exhausted(self):
        from apps.pii.test_liquid_windows import FakeClock, LiquidWindowTests

        for background in (False, True):
            clock = FakeClock()
            pipeline, _, _ = LiquidWindowTests().pipeline(clock=clock, durations=[float("inf")])
            text = "John Smith " + "a " * 2000
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "pii.sock")
                # Advance only the model's clock; the transport remains open.
                with (
                    patch("apps.pii.liquid_engine.time", clock),
                    self.assertLogs("apps.pii.shared_server", level="INFO") as logs,
                    _running_server(path, pipeline, engine="liquid"),
                ):
                    result = SharedPiiPipeline(engine="liquid", socket_path=path)(text, background=background)
                self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10)])
                lines = [line for line in logs.output if "window_deadline" in line]
                self.assertEqual(len(lines), 1)
                self.assertIn("windows_completed=1 windows_total=3", lines[0])
                self.assertNotIn("John", lines[0])

    def test_request_read_time_is_not_added_back_to_client_ttl(self):
        from types import SimpleNamespace

        from apps.pii.shared_server import SharedDetectorServer
        from apps.pii.test_liquid_windows import FakeClock

        clock = FakeClock(100.0)
        server = SharedDetectorServer(engine="liquid", configure_runtime=False)
        server.ready.set()
        jobs = []

        def enqueue(job):
            jobs.append(job)
            job.response = {"v": 1, "engine": "liquid", "spans": []}
            job.done.set()

        def read_request(connection):
            clock.now += 2.0
            return {"v": 1, "engine": "liquid", "text": "synthetic", "ttl_ms": 5000}

        server._jobs = SimpleNamespace(put_nowait=enqueue, qsize=lambda: 0)
        with (
            patch("apps.pii.shared_server.time", clock),
            patch("apps.pii.shared_server._read_request", side_effect=read_request),
            patch.object(server, "_send"),
        ):
            server._handle_connection(Mock())
        self.assertEqual(jobs[0].deadline, 105.0)
